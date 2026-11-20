"""
Serveur Web FastAPI pour l'Add-on Home Assistant E.Leclerc Drive AI.
Supporte Ingress (interface intégrée au menu latéral Home Assistant),
l'API REST locale, le streaming SSE des logs en direct et la synchronisation
automatique avec les entités et notifications de Home Assistant via SUPERVISOR_TOKEN.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

import requests
from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from leclerc_engine import LeclercEngine, fetch_google_keep_items, resolve_google_keep_master_token

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
_LOGGER = logging.getLogger("leclerc_addon")

DATA_DIR = Path("/data") if Path("/data").exists() else Path(__file__).resolve().parent / "local_data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
OPTIONS_FILE = Path("/data/options.json")
SETTINGS_FILE = DATA_DIR / "settings.json"

LOG_BUFFER: list[str] = []
LOG_SUBSCRIBERS: list[asyncio.Queue] = []
RUN_LOCK = threading.Lock()
IS_RUNNING = False

app = FastAPI(title="E.Leclerc Drive AI Add-on")


def broadcast_log(msg: str) -> None:
    timestamp = datetime.now().strftime("%H:%M:%S")
    entry = f"[{timestamp}] {msg}"
    LOG_BUFFER.append(entry)
    if len(LOG_BUFFER) > 500:
        LOG_BUFFER.pop(0)
    for q in list(LOG_SUBSCRIBERS):
        try:
            q.put_nowait(entry)
        except Exception:
            pass


engine = LeclercEngine(data_dir=DATA_DIR, log_callback=broadcast_log)


def load_settings() -> dict[str, Any]:
    cfg: dict[str, Any] = {
        "leclerc_email": "",
        "leclerc_password": "",
        "magasin_slug": "",
        "list_source": "google_keep",
        "google_email": "",
        "google_app_password": "",
        "google_master_token": "",
        "keep_note_title": "Liste de courses",
        "todo_entity_id": "todo.shopping_list",
        "ai_provider": "ollama",
        "conversation_entity": "",
        "ollama_url": "http://homeassistant.local:11434",
        "ollama_model": "qwen2.5:1.5b",
        "plural_items_count": 3,
    }
    if OPTIONS_FILE.exists():
        try:
            cfg.update(json.loads(OPTIONS_FILE.read_text(encoding="utf-8")))
        except Exception as e:
            _LOGGER.warning("Erreur lecture options.json : %s", e)

    if SETTINGS_FILE.exists():
        try:
            cfg.update(json.loads(SETTINGS_FILE.read_text(encoding="utf-8")))
        except Exception as e:
            _LOGGER.warning("Erreur lecture settings.json : %s", e)

    return cfg


def save_settings(new_cfg: dict[str, Any]) -> None:
    cur = load_settings()
    cur.update(new_cfg)
    SETTINGS_FILE.write_text(json.dumps(cur, indent=2, ensure_ascii=False), encoding="utf-8")


def call_ha_api(endpoint: str, method: str = "GET", payload: dict | None = None) -> Any:
    token = os.getenv("SUPERVISOR_TOKEN")
    if not token:
        return None
    url = f"http://supervisor/core/api/{endpoint.lstrip('/')}"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    try:
        if method.upper() == "POST":
            r = requests.post(url, json=payload or {}, headers=headers, timeout=15)
        else:
            r = requests.get(url, headers=headers, timeout=15)
        if r.status_code in (200, 201):
            return r.json()
    except Exception as e:
        _LOGGER.debug("Erreur appel HA Core API (%s) : %s", endpoint, e)
    return None


def update_ha_sensors(cart_total: str, cart_count: int, report: dict) -> None:
    call_ha_api("states/sensor.leclerc_drive_panier", "POST", {
        "state": cart_total,
        "attributes": {
            "friendly_name": "Panier E.Leclerc Actuel",
            "icon": "mdi:cart",
            "articles_dans_le_panier": cart_count,
            "mode_courses": report.get("mode_courses"),
            "saison_france": report.get("saison_france"),
        }
    })

    rem = report.get("rappels_oublis_habituels", [])
    doubts = report.get("doutes_et_arbitrages", [])
    missing = report.get("missing", [])
    call_ha_api("states/sensor.leclerc_drive_rappels_doutes", "POST", {
        "state": f"{len(missing)} manquant(s) | {len(doubts)} doute(s) | {len(rem)} rappel(s)",
        "attributes": {
            "friendly_name": "Rappels & Doutes Courses E.Leclerc",
            "icon": "mdi:bell-ring",
            "articles_manquants": missing,
            "doutes_et_arbitrages": doubts,
            "rappels_oublis_habituels": [f"{r.get('name')} ({r.get('reason')})" for r in rem if isinstance(r, dict)],
        }
    })


def create_ha_notification(report: dict, cart_count: int, cart_total: str, avg_basket: float) -> None:
    lines = [
        f"**Panier E.Leclerc actuel :** {cart_count} article(s) ({cart_total}) — *Moyenne historique : {avg_basket} €*",
        f"**Saison (France) :** {report.get('saison_france')}",
        f"**Rythme détecté :** {report.get('mode_courses')}",
        "",
        f"### ✅ Produits ajoutés au panier ({len(report.get('added', []))}) :",
    ]
    for a in report.get("added", []):
        lines.append(f"- **{a['item']}** → {a['product']} (x{a['qty']} — {a['price']})")

    if report.get("missing"):
        lines.append(f"\n### ❌ Produits manquants / introuvables ({len(report['missing'])}) :")
        for m in report["missing"]:
            lines.append(f"- **{m['item']}** (*recherche : {m['query']}*) : {m['reason']}")

    if report.get("doutes_et_arbitrages"):
        lines.append(f"\n### 🤔 Doutes & Arbitrages de l'IA ({len(report['doutes_et_arbitrages'])}) :")
        for d in report["doutes_et_arbitrages"]:
            lines.append(f"- {d}")

    if report.get("rappels_oublis_habituels"):
        lines.append(f"\n### 🔔 Rappels : Produits habituels oubliés ({len(report['rappels_oublis_habituels'])}) :")
        for r in report["rappels_oublis_habituels"]:
            lines.append(f"- **{r['name']}** — *{r['reason']}*")

    call_ha_api("services/persistent_notification/create", "POST", {
        "title": "🛒 Courses E.Leclerc Drive terminées",
        "message": "\n".join(lines),
        "notification_id": "leclerc_drive_cart_report",
    })


def fetch_shopping_items(cfg: dict) -> list[str]:
    source = cfg.get("list_source", "google_keep")
    if source == "google_keep":
        g_email = cfg.get("google_email", "").strip()
        master_tok = cfg.get("google_master_token", "").strip()
        app_pwd = cfg.get("google_app_password", "").strip()
        title = cfg.get("keep_note_title", "Liste de courses")

        if not master_tok and app_pwd:
            try:
                master_tok = resolve_google_keep_master_token(g_email, app_pwd)
                cfg["google_master_token"] = master_tok
                save_settings({"google_master_token": master_tok})
            except Exception as e:
                broadcast_log(f"⚠️ Erreur échange token Google Keep : {e}")

        if g_email and master_tok:
            broadcast_log(f"📝 Lecture de la note Google Keep '{title}'...")
            return fetch_google_keep_items(g_email, master_tok, title)
        return []

    # Source Home Assistant Todo
    todo_entity = cfg.get("todo_entity_id", "todo.shopping_list")
    broadcast_log(f"📝 Lecture de la liste Todo Home Assistant '{todo_entity}'...")
    res = call_ha_api(f"services/todo/get_items", "POST", {"entity_id": todo_entity, "status": ["needs_action"]})
    if res and isinstance(res, dict):
        items = res.get(todo_entity, {}).get("items", [])
        return [str(i.get("summary", "")).strip() for i in items if i.get("summary")]
    return []


def call_ai_planner(items_to_translate: list[str], all_items: list[str], stats: dict, season_ctx: dict, cfg: dict) -> tuple[list[dict], list[str]]:
    top_products = [
        {"name": p.get("name"), "achats": p.get("occurrences"), "dans_derniere_commande": p.get("in_last_order")}
        for p in stats.get("products", [])[:25]
    ]
    prompt = (
        "Tu es un assistant courses E.Leclerc Drive intelligent en France.\n"
        f"- Saison actuelle en France : {season_ctx['summary']}\n"
        f"- Légumes de saison : {', '.join(season_ctx['seasonal_veg'][:7])}\n"
        f"- Fruits de saison : {', '.join(season_ctx['seasonal_fruits'][:6])}\n"
        f"- Dernière commande : {stats.get('last_order_date')} (il y a {stats.get('days_since_last_order')}j, moyenne {stats.get('avg_days_between_orders')}j)\n"
        f"- Mode de courses : {stats.get('shopping_mode_label')}\n"
        f"- Articles déjà achetés à la dernière commande : {json.dumps(stats.get('last_order_items', [])[:12], ensure_ascii=False)}\n"
        f"- Articles demandés aujourd'hui : {json.dumps(all_items, ensure_ascii=False)}\n"
        f"- Articles restant à traduire : {json.dumps(items_to_translate, ensure_ascii=False)}\n"
        f"- Historique foyer : {json.dumps(top_products, ensure_ascii=False)}\n\n"
        "Réponds UNIQUEMENT en JSON : "
        '{"plan": [{"keep_item": "...", "search_query": "...", "preferred_leclerc_id": null, "qty": 1}], '
        '"doutes_ia": ["remarque 1 sur la rotation ou la saison..."]}'
    )

    conv_entity = cfg.get("conversation_entity")
    if cfg.get("ai_provider") == "conversation_entity" and conv_entity:
        try:
            broadcast_log(f"🤖 Consultation de l'agent Home Assistant '{conv_entity}'...")
            res = call_ha_api("services/conversation/process", "POST", {"agent_id": conv_entity, "text": prompt})
            speech = (res or {}).get("response", {}).get("speech", {}).get("plain", {}).get("speech", "")
            m = re.search(r"\{.*\}", speech, re.DOTALL)
            if m:
                parsed = json.loads(m.group(0))
                return parsed.get("plan", []), parsed.get("doutes_ia", [])
        except Exception as e:
            broadcast_log(f"⚠️ Erreur entité conversation HA ({e}), repli Ollama")

    # Ollama par défaut
    ollama_url = cfg.get("ollama_url", "http://homeassistant.local:11434").rstrip("/")
    model = cfg.get("ollama_model", "qwen2.5:1.5b")
    try:
        broadcast_log(f"🤖 Consultation Ollama ({model})...")
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "format": "json",
            "stream": False,
            "keep_alive": 0,
            "options": {"temperature": 0.1, "num_ctx": 3072},
        }
        r = requests.post(f"{ollama_url}/api/chat", json=payload, timeout=90)
        content = r.json().get("message", {}).get("content", "{}")
        parsed = json.loads(content)
        return parsed.get("plan", []), parsed.get("doutes_ia", [])
    except Exception as e:
        broadcast_log(f"⚠️ Ollama indisponible ({e}), repli 100% Python")
        return [], []


def run_shopping_flow() -> None:
    global IS_RUNNING
    with RUN_LOCK:
        IS_RUNNING = True
        try:
            cfg = load_settings()
            engine.set_store(cfg.get("magasin_slug", ""))
            engine.plural_count = int(cfg.get("plural_items_count", 3) or 3)

            broadcast_log("🚀 Démarrage du remplissage intelligent du panier...")
            raw_items = fetch_shopping_items(cfg)
            if not raw_items:
                broadcast_log("⚠️ Liste de courses vide. Aucune action effectuée.")
                return

            broadcast_log(f"📋 {len(raw_items)} article(s) trouvé(s) sur la liste.")
            stats = engine.compute_stats(300)
            season_ctx = engine.get_french_season_context()
            products = stats.get("products", [])

            broadcast_log(f"📊 Mode détecté : {stats.get('shopping_mode_label')}")
            broadcast_log(f"🍂 Saison : {season_ctx['summary']}")

            shopping_plan: list[dict] = []
            items_needing_ai: list[str] = []
            arbitrations: list[str] = []

            for raw in raw_items:
                words = set(raw.lower().split())
                is_gen = bool(words & {
                    "legume", "legumes", "fruit", "fruits", "viande", "viandes",
                    "fromage", "fromages", "yaourt", "yaourts", "charcuterie"
                })

                if is_gen:
                    family = engine.find_semantic_family_products(raw, products, stats=stats, arbitrations_out=arbitrations)
                    if family:
                        for fp in family:
                            sq = fp["name"].split(" - ")[0].strip()
                            q = max(1, int(fp.get("suggested_qty", 1) or 1))
                            if stats.get("shopping_mode") == "giga_courses_frigo_a_sec" and fp.get("occurrences", 0) >= 6:
                                q = min(3, q + 1)
                            shopping_plan.append({"keep_item": raw, "search_query": sq, "preferred_leclerc_id": fp.get("leclerc_id"), "qty": q})
                    else:
                        items_needing_ai.append(raw)
                else:
                    match = engine.find_history_match(raw, products)
                    if match:
                        sq = match["name"].split(" - ")[0].strip()
                        if match.get("in_last_order") and (stats.get("days_since_last_order") or 99) <= 7:
                            arbitrations.append(f"🤔 '{raw}' déjà acheté à la dernière commande ({stats.get('last_order_date')}), maintenu car noté explicitement.")
                        shopping_plan.append({"keep_item": raw, "search_query": sq, "preferred_leclerc_id": match.get("leclerc_id"), "qty": max(1, int(match.get("suggested_qty", 1) or 1))})
                    else:
                        arbitrations.append(f"🤔 Article inédit '{raw}' : absent de l'historique, recherche directe sans substitution.")
                        shopping_plan.append({"keep_item": raw, "search_query": raw, "preferred_leclerc_id": None, "qty": 1})

            if items_needing_ai:
                ai_plan, ai_doubts = call_ai_planner(items_needing_ai, raw_items, stats, season_ctx, cfg)
                for d in ai_doubts:
                    arbitrations.append(f"🤖 Audit IA : {d}")
                for entry in ai_plan:
                    q = str(entry.get("search_query", "")).strip()
                    if q:
                        shopping_plan.append({"keep_item": entry.get("keep_item", ""), "search_query": q, "preferred_leclerc_id": entry.get("preferred_leclerc_id"), "qty": max(1, int(entry.get("qty", 1) or 1))})

            reminders = engine.detect_forgotten_staples(shopping_plan, stats)

            # Connexion et Ajout
            conn_res = engine.login_leclerc(cfg.get("leclerc_email", ""), cfg.get("leclerc_password", ""))
            if conn_res.get("status") != "connected":
                broadcast_log(f"❌ Échec connexion : {conn_res.get('message', 'Non connecté')}")
                return

            added = []
            missing = []
            cart_total = conn_res.get("cart_total", "0,00 €")
            cart_count = int(conn_res.get("cart_count", 0))

            for step in shopping_plan:
                res = engine.add_product_by_query(query=step["search_query"], qty=step["qty"], preferred_leclerc_id=step.get("preferred_leclerc_id"))
                if res.get("status") == "added":
                    added.append({"item": step["keep_item"], "product": res.get("selected_product"), "price": res.get("price"), "qty": res.get("qty")})
                    cart_total = res.get("cart_total_after", cart_total)
                    cart_count = int(res.get("cart_count_after", cart_count))
                    broadcast_log(f"  ✅ Ajouté : {res.get('selected_product')} (x{res.get('qty')} - {res.get('price')}) | Panier : {cart_total}")
                else:
                    missing.append({"item": step["keep_item"], "query": step["search_query"], "reason": res.get("reason", "Introuvable")})
                    broadcast_log(f"  ❌ Non trouvé : '{step['keep_item']}' ({res.get('reason')})")

            report = {
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "saison_france": season_ctx["summary"],
                "derniere_commande": stats.get("last_order_date"),
                "mode_courses": stats.get("shopping_mode_label"),
                "added": added,
                "missing": missing,
                "doutes_et_arbitrages": arbitrations,
                "rappels_oublis_habituels": reminders,
                "cart_total": cart_total,
                "cart_count": cart_count,
            }
            (DATA_DIR / "last_report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

            update_ha_sensors(cart_total, cart_count, report)
            create_ha_notification(report, cart_count, cart_total, stats.get("avg_basket_eur", 0.0))
            broadcast_log(f"🎉 Terminé avec succès ! {len(added)} article(s) ajouté(s), {len(missing)} introuvable(s).")
        except Exception as e:
            broadcast_log(f"❌ Erreur critique pendant les courses : {e}")
        finally:
            engine.close()
            IS_RUNNING = False


@app.get("/", response_class=HTMLResponse)
async def serve_ui(request: Request):
    root_path = request.headers.get("X-Ingress-Path", "").rstrip("/")
    stats = engine.compute_stats(50)
    season = engine.get_french_season_context()
    rep_file = DATA_DIR / "last_report.json"
    last_report = json.loads(rep_file.read_text(encoding="utf-8")) if rep_file.exists() else {}
    settings = load_settings()

    html = f"""<!DOCTYPE html>
<html lang="fr" class="dark">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>E.Leclerc Drive AI</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        body {{ background: #0f172a; color: #f8fafc; font-family: system-ui, -apple-system, sans-serif; }}
        .badge {{ display: inline-flex; align-items: center; padding: 0.2rem 0.6rem; border-radius: 9999px; font-size: 0.75rem; font-weight: 600; }}
    </style>
</head>
<body class="p-4 md:p-6 max-w-6xl mx-auto">
    <!-- Header -->
    <header class="flex flex-col md:flex-row md:items-center justify-between pb-6 border-b border-slate-800 gap-4">
        <div>
            <h1 class="text-2xl md:text-3xl font-extrabold text-blue-400 flex items-center gap-2">
                🛒 E.Leclerc Drive AI
                <span class="badge bg-blue-900 text-blue-200 text-xs">Add-on Ingress</span>
            </h1>
            <p class="text-sm text-slate-400 mt-1">Assistant intelligent avec rotation des viandes, saisonnalité & anti-bridage</p>
        </div>
        <div class="flex items-center gap-2">
            <button onclick="triggerRun()" id="btnRun" class="bg-blue-600 hover:bg-blue-500 text-white font-bold py-2.5 px-5 rounded-xl shadow-lg transition flex items-center gap-2">
                ⚡ Lancer les courses
            </button>
            <button onclick="triggerSync()" id="btnSync" class="bg-slate-800 hover:bg-slate-700 text-slate-200 font-semibold py-2.5 px-4 rounded-xl border border-slate-700 transition">
                🔄 Sync Historique
            </button>
        </div>
    </header>

    <!-- Stat Cards -->
    <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4 my-6">
        <div class="bg-slate-800/60 p-4 rounded-2xl border border-slate-700/60">
            <span class="text-xs text-slate-400 font-semibold uppercase tracking-wider">Panier Actuel</span>
            <div class="text-2xl font-black text-emerald-400 mt-1" id="statCartTotal">{last_report.get('cart_total', '0,00 €')}</div>
            <span class="text-xs text-slate-400" id="statCartCount">{last_report.get('cart_count', 0)} article(s) dans le panier</span>
        </div>
        <div class="bg-slate-800/60 p-4 rounded-2xl border border-slate-700/60">
            <span class="text-xs text-slate-400 font-semibold uppercase tracking-wider">Panier Moyen Historique</span>
            <div class="text-2xl font-black text-blue-300 mt-1">{stats.get('avg_basket_eur', 0.0)} €</div>
            <span class="text-xs text-slate-400">{stats.get('orders_count', 0)} commandes analysées</span>
        </div>
        <div class="bg-slate-800/60 p-4 rounded-2xl border border-slate-700/60">
            <span class="text-xs text-slate-400 font-semibold uppercase tracking-wider">Rythme & Mode</span>
            <div class="text-base font-bold text-amber-300 mt-1 leading-snug">{stats.get('shopping_mode', 'Normal')}</div>
            <span class="text-xs text-slate-400">Dernier achat : {stats.get('last_order_date', 'Inconnu')} ({stats.get('days_since_last_order')}j)</span>
        </div>
        <div class="bg-slate-800/60 p-4 rounded-2xl border border-slate-700/60">
            <span class="text-xs text-slate-400 font-semibold uppercase tracking-wider">Saison (France)</span>
            <div class="text-base font-bold text-teal-300 mt-1">{season['summary']}</div>
            <span class="text-xs text-slate-400">Filtre anti-hors-saison actif</span>
        </div>
    </div>

    <!-- Navigation Tabs -->
    <div class="flex border-b border-slate-800 mb-6 gap-2">
        <button onclick="switchTab('report')" id="tab-report" class="py-2.5 px-4 font-semibold text-blue-400 border-b-2 border-blue-500">📋 Rapport & Résultats</button>
        <button onclick="switchTab('logs')" id="tab-logs" class="py-2.5 px-4 font-semibold text-slate-400 hover:text-slate-200">📜 Journal en direct</button>
        <button onclick="switchTab('browser')" id="tab-browser" class="py-2.5 px-4 font-semibold text-slate-400 hover:text-slate-200">🖥️ Vue Drive & Session</button>
        <button onclick="switchTab('settings')" id="tab-settings" class="py-2.5 px-4 font-semibold text-slate-400 hover:text-slate-200">⚙️ Configuration</button>
    </div>

    <!-- TAB 1: Report -->
    <div id="content-report" class="space-y-6">
        <div class="grid grid-cols-1 md:grid-cols-2 gap-6">
            <!-- Added Items -->
            <div class="bg-slate-900/80 p-5 rounded-2xl border border-emerald-900/40">
                <h3 class="text-lg font-bold text-emerald-400 flex items-center gap-2 mb-3">
                    <span>✅</span> Produits ajoutés au panier ({len(last_report.get('added', []))})
                </h3>
                <div class="space-y-2 max-h-80 overflow-y-auto pr-1">
                    {''.join([f'''<div class="p-2.5 bg-slate-800/70 rounded-xl text-sm flex justify-between items-center">
                        <div>
                            <span class="font-semibold text-slate-200">{a['item']}</span>
                            <div class="text-xs text-slate-400">{a['product']}</div>
                        </div>
                        <span class="badge bg-emerald-950 text-emerald-300 border border-emerald-700/50">x{a['qty']} — {a['price']}</span>
                    </div>''' for a in last_report.get('added', [])]) or '<p class="text-sm text-slate-500 italic">Aucun article ajouté pour le moment.</p>'}
                </div>
            </div>

            <!-- Missing Items -->
            <div class="bg-slate-900/80 p-5 rounded-2xl border border-rose-900/40">
                <h3 class="text-lg font-bold text-rose-400 flex items-center gap-2 mb-3">
                    <span>❌</span> Produits introuvables sur le Drive ({len(last_report.get('missing', []))})
                </h3>
                <div class="space-y-2 max-h-80 overflow-y-auto pr-1">
                    {''.join([f'''<div class="p-2.5 bg-slate-800/70 rounded-xl text-sm border-l-2 border-rose-500">
                        <span class="font-semibold text-slate-200">{m['item']}</span>
                        <div class="text-xs text-rose-300 mt-0.5">{m['reason']} (Non remplacé par erreur)</div>
                    </div>''' for m in last_report.get('missing', [])]) or '<p class="text-sm text-slate-500 italic">Aucun produit manquant.</p>'}
                </div>
            </div>
        </div>

        <div class="grid grid-cols-1 md:grid-cols-2 gap-6">
            <!-- Doubts & Arbitrations -->
            <div class="bg-slate-900/80 p-5 rounded-2xl border border-amber-900/40">
                <h3 class="text-lg font-bold text-amber-300 flex items-center gap-2 mb-3">
                    <span>🤔</span> Doutes & Arbitrages de l'IA ({len(last_report.get('doutes_et_arbitrages', []))})
                </h3>
                <div class="space-y-2 max-h-72 overflow-y-auto pr-1">
                    {''.join([f'''<div class="p-2.5 bg-slate-800/70 rounded-xl text-xs text-amber-200/90 leading-relaxed">
                        {d}
                    </div>''' for d in last_report.get('doutes_et_arbitrages', [])]) or '<p class="text-sm text-slate-500 italic">Aucun arbitrage particulier.</p>'}
                </div>
            </div>

            <!-- Reminders -->
            <div class="bg-slate-900/80 p-5 rounded-2xl border border-blue-900/40">
                <h3 class="text-lg font-bold text-blue-300 flex items-center gap-2 mb-3">
                    <span>🔔</span> Rappels : Produits habituels oubliés ({len(last_report.get('rappels_oublis_habituels', []))})
                </h3>
                <div class="space-y-2 max-h-72 overflow-y-auto pr-1">
                    {''.join([f'''<div class="p-2.5 bg-slate-800/70 rounded-xl text-xs flex justify-between items-center gap-2">
                        <span class="font-semibold text-slate-200">{r.get('name')}</span>
                        <span class="text-slate-400 text-right">{r.get('reason')}</span>
                    </div>''' for r in last_report.get('rappels_oublis_habituels', [])]) or '<p class="text-sm text-slate-500 italic">Aucun rappel d\'oubli pour cette commande.</p>'}
                </div>
            </div>
        </div>
    </div>

    <!-- TAB 2: Live Logs -->
    <div id="content-logs" class="hidden">
        <div class="bg-slate-950 p-4 rounded-2xl border border-slate-800 font-mono text-xs text-slate-300 h-96 overflow-y-auto shadow-inner space-y-1" id="logBox">
            {''.join([f'<div>{line}</div>' for line in LOG_BUFFER]) or '<div>En attente d\'activité...</div>'}
        </div>
    </div>

    <!-- TAB 3: Browser & Captcha -->
    <div id="content-browser" class="hidden space-y-6">
        <div class="bg-slate-900/80 p-5 rounded-2xl border border-slate-800">
            <h3 class="text-lg font-bold text-slate-200 mb-3">📸 Dernière capture du navigateur Drive</h3>
            <div class="rounded-xl overflow-hidden border border-slate-700 bg-black max-h-[460px] flex items-center justify-center">
                <img id="browserScreenshot" src="{root_path}/api/screenshot?t={int(datetime.now().timestamp())}" alt="Capture Drive" class="object-contain max-h-[450px] w-full" onerror="this.src='data:image/svg+xml,<svg xmlns=\\'http://www.w3.org/2000/svg\\' width=\\'400\\' height=\\'200\\'><text x=\\'50%\\' y=\\'50%\\' fill=\\'%2364748b\\' text-anchor=\\'middle\\'>Aucune capture disponible</text></svg>'">
            </div>
            <button onclick="refreshScreenshot()" class="mt-3 bg-slate-800 hover:bg-slate-700 text-xs text-slate-300 py-1.5 px-3 rounded-lg border border-slate-700">🔄 Rafraîchir l'image</button>
        </div>

        <div class="grid grid-cols-1 md:grid-cols-2 gap-6">
            <!-- OTP Form -->
            <div class="bg-slate-900/80 p-5 rounded-2xl border border-slate-800">
                <h4 class="font-bold text-slate-200 mb-2">📩 Code de sécurité e-mail (6 chiffres)</h4>
                <p class="text-xs text-slate-400 mb-3">Si E.Leclerc envoie un code à 6 chiffres par e-mail, entrez-le ici :</p>
                <div class="flex gap-2">
                    <input type="text" id="otpCode" placeholder="Ex: 123456" maxlength="6" class="bg-slate-800 border border-slate-700 rounded-xl px-3 py-2 text-sm text-white w-36 text-center tracking-widest font-mono">
                    <button onclick="submitOtp()" class="bg-emerald-600 hover:bg-emerald-500 text-white font-bold px-4 py-2 rounded-xl text-sm transition">Valider l'OTP</button>
                </div>
            </div>

            <!-- Cookie / cURL Session Injection -->
            <div class="bg-slate-900/80 p-5 rounded-2xl border border-slate-800">
                <h4 class="font-bold text-slate-200 mb-2">🍪 Déblocage Anti-Captcha (Session / cURL)</h4>
                <p class="text-xs text-slate-400 mb-3">Si un Captcha bloque, collez votre session (cURL, document.cookie ou jeton) :</p>
                <textarea id="sessionTokenInput" rows="2" placeholder="Collez ici votre session..." class="w-full bg-slate-800 border border-slate-700 rounded-xl p-2.5 text-xs text-slate-200 font-mono"></textarea>
                <button onclick="importSessionToken()" class="mt-2 bg-blue-600 hover:bg-blue-500 text-white font-bold px-4 py-2 rounded-xl text-xs transition">Importer la session</button>
            </div>
        </div>
    </div>

    <!-- TAB 4: Settings -->
    <div id="content-settings" class="hidden">
        <form id="settingsForm" onsubmit="saveSettingsForm(event)" class="bg-slate-900/80 p-6 rounded-2xl border border-slate-800 space-y-5">
            <h3 class="text-lg font-bold text-slate-200 border-b border-slate-800 pb-2">⚙️ Identifiants et Options</h3>
            <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
                <div>
                    <label class="block text-xs font-semibold text-slate-400 mb-1">E-mail E.Leclerc Drive</label>
                    <input type="email" name="leclerc_email" value="{settings.get('leclerc_email', '')}" class="w-full bg-slate-800 border border-slate-700 rounded-xl p-2.5 text-sm text-white">
                </div>
                <div>
                    <label class="block text-xs font-semibold text-slate-400 mb-1">Mot de passe E.Leclerc Drive</label>
                    <input type="password" name="leclerc_password" value="{settings.get('leclerc_password', '')}" class="w-full bg-slate-800 border border-slate-700 rounded-xl p-2.5 text-sm text-white">
                </div>
                <div>
                    <label class="block text-xs font-semibold text-slate-400 mb-1">Magasin / Ville / Code Postal (Optionnel)</label>
                    <input type="text" name="magasin_slug" value="{settings.get('magasin_slug', '')}" placeholder="Ex: 54500 ou magasin-125401..." class="w-full bg-slate-800 border border-slate-700 rounded-xl p-2.5 text-sm text-white">
                </div>
                <div>
                    <label class="block text-xs font-semibold text-slate-400 mb-1">Source de la liste de courses</label>
                    <select name="list_source" class="w-full bg-slate-800 border border-slate-700 rounded-xl p-2.5 text-sm text-white">
                        <option value="google_keep" {"selected" if settings.get("list_source") == "google_keep" else ""}>Google Keep</option>
                        <option value="ha_todo" {"selected" if settings.get("list_source") == "ha_todo" else ""}>Home Assistant Todo (todo.*)</option>
                    </select>
                </div>
                <div>
                    <label class="block text-xs font-semibold text-slate-400 mb-1">E-mail Gmail (si Google Keep)</label>
                    <input type="email" name="google_email" value="{settings.get('google_email', '')}" class="w-full bg-slate-800 border border-slate-700 rounded-xl p-2.5 text-sm text-white">
                </div>
                <div>
                    <label class="block text-xs font-semibold text-slate-400 mb-1">Mot de passe d'application Google (16 lettres)</label>
                    <input type="password" name="google_app_password" value="{settings.get('google_app_password', '')}" class="w-full bg-slate-800 border border-slate-700 rounded-xl p-2.5 text-sm text-white">
                </div>
                <div>
                    <label class="block text-xs font-semibold text-slate-400 mb-1">Fournisseur IA</label>
                    <select name="ai_provider" class="w-full bg-slate-800 border border-slate-700 rounded-xl p-2.5 text-sm text-white">
                        <option value="ollama" {"selected" if settings.get("ai_provider") == "ollama" else ""}>Ollama (Local / qwen2.5:1.5b)</option>
                        <option value="conversation_entity" {"selected" if settings.get("ai_provider") == "conversation_entity" else ""}>Entité Conversation Home Assistant</option>
                    </select>
                </div>
                <div>
                    <label class="block text-xs font-semibold text-slate-400 mb-1">Entité HA (si conversation.* sélectionné)</label>
                    <input type="text" name="conversation_entity" value="{settings.get('conversation_entity', '')}" placeholder="conversation.home_assistant" class="w-full bg-slate-800 border border-slate-700 rounded-xl p-2.5 text-sm text-white">
                </div>
                <div>
                    <label class="block text-xs font-semibold text-slate-400 mb-1">URL Ollama</label>
                    <input type="text" name="ollama_url" value="{settings.get('ollama_url', 'http://homeassistant.local:11434')}" class="w-full bg-slate-800 border border-slate-700 rounded-xl p-2.5 text-sm text-white">
                </div>
                <div>
                    <label class="block text-xs font-semibold text-slate-400 mb-1">Modèle Ollama</label>
                    <input type="text" name="ollama_model" value="{settings.get('ollama_model', 'qwen2.5:1.5b')}" class="w-full bg-slate-800 border border-slate-700 rounded-xl p-2.5 text-sm text-white">
                </div>
            </div>
            <div class="pt-2">
                <button type="submit" class="bg-blue-600 hover:bg-blue-500 text-white font-bold px-6 py-2.5 rounded-xl transition">💾 Enregistrer les paramètres</button>
            </div>
        </form>
    </div>

    <script>
        const rootPath = '{root_path}';

        function switchTab(tabId) {{
            ['report', 'logs', 'browser', 'settings'].forEach(t => {{
                document.getElementById('content-' + t).classList.add('hidden');
                document.getElementById('tab-' + t).classList.remove('text-blue-400', 'border-b-2', 'border-blue-500');
                document.getElementById('tab-' + t).classList.add('text-slate-400');
            }});
            document.getElementById('content-' + tabId).classList.remove('hidden');
            document.getElementById('tab-' + tabId).classList.add('text-blue-400', 'border-b-2', 'border-blue-500');
            document.getElementById('tab-' + tabId).classList.remove('text-slate-400');
        }}

        function triggerRun() {{
            const btn = document.getElementById('btnRun');
            btn.disabled = true;
            btn.innerText = '⏳ En cours...';
            switchTab('logs');
            fetch(rootPath + '/api/run', {{ method: 'POST' }})
                .then(r => r.json())
                .then(d => alert(d.message || 'Courses lancées !'))
                .finally(() => {{ btn.disabled = false; btn.innerText = '⚡ Lancer les courses'; }});
        }}

        function triggerSync() {{
            const btn = document.getElementById('btnSync');
            btn.disabled = true;
            btn.innerText = '⏳ Synchronisation...';
            switchTab('logs');
            fetch(rootPath + '/api/sync_history', {{ method: 'POST' }})
                .then(r => r.json())
                .then(d => alert(d.message || 'Historique synchronisé !'))
                .finally(() => {{ btn.disabled = false; btn.innerText = '🔄 Sync Historique'; }});
        }}

        function submitOtp() {{
            const code = document.getElementById('otpCode').value;
            fetch(rootPath + '/api/otp', {{
                method: 'POST',
                headers: {{ 'Content-Type': 'application/json' }},
                body: JSON.stringify({{ code }})
            }}).then(r => r.json()).then(d => alert(d.message));
        }}

        function importSessionToken() {{
            const token = document.getElementById('sessionTokenInput').value;
            fetch(rootPath + '/api/session_token', {{
                method: 'POST',
                headers: {{ 'Content-Type': 'application/json' }},
                body: JSON.stringify({{ token }})
            }}).then(r => r.json()).then(d => alert(d.message));
        }}

        function refreshScreenshot() {{
            document.getElementById('browserScreenshot').src = rootPath + '/api/screenshot?t=' + Date.now();
        }}

        function saveSettingsForm(e) {{
            e.preventDefault();
            const data = Object.fromEntries(new FormData(e.target).entries());
            fetch(rootPath + '/api/settings', {{
                method: 'POST',
                headers: {{ 'Content-Type': 'application/json' }},
                body: JSON.stringify(data)
            }}).then(r => r.json()).then(d => alert(d.message));
        }}

        // Live Log SSE
        const evtSource = new EventSource(rootPath + '/api/logs');
        evtSource.onmessage = function(e) {{
            const box = document.getElementById('logBox');
            const div = document.createElement('div');
            div.innerText = e.data;
            box.appendChild(div);
            box.scrollTop = box.scrollHeight;
        }};
    </script>
</body>
</html>"""
    return HTMLResponse(content=html)


@app.get("/api/status")
async def get_status():
    rep_file = DATA_DIR / "last_report.json"
    last_report = json.loads(rep_file.read_text(encoding="utf-8")) if rep_file.exists() else {}
    return {
        "is_running": IS_RUNNING,
        "history": engine.compute_stats(50),
        "season": engine.get_french_season_context(),
        "last_report": last_report,
    }


@app.post("/api/run")
async def trigger_run():
    global IS_RUNNING
    if IS_RUNNING:
        return JSONResponse({"status": "busy", "message": "Une commande est déjà en cours d'exécution."}, status_code=409)
    threading.Thread(target=run_shopping_flow, daemon=True).start()
    return {"status": "started", "message": "Courses E.Leclerc Drive démarrées en arrière-plan !"}


@app.post("/api/sync_history")
async def trigger_sync():
    global IS_RUNNING
    if IS_RUNNING:
        return JSONResponse({"status": "busy", "message": "Une tâche est déjà en cours."}, status_code=409)

    def _sync():
        global IS_RUNNING
        with RUN_LOCK:
            IS_RUNNING = True
            try:
                engine.scrape_order_history(15)
            finally:
                IS_RUNNING = False

    threading.Thread(target=_sync, daemon=True).start()
    return {"status": "started", "message": "Synchronisation de l'historique démarrée !"}


@app.post("/api/otp")
async def api_submit_otp(payload: dict):
    code = payload.get("code", "")
    res = engine.submit_otp(code)
    return res


@app.post("/api/session_token")
async def api_import_token(payload: dict):
    tok = payload.get("token", "")
    count = engine.import_session_cookies(tok)
    if count > 0:
        return {"status": "ok", "message": f"{count} cookie(s) importé(s) avec succès !"}
    return {"status": "error", "message": "Aucun cookie valide détecté."}


@app.post("/api/settings")
async def api_save_settings(payload: dict):
    save_settings(payload)
    return {"status": "ok", "message": "Paramètres enregistrés avec succès !"}


@app.get("/api/screenshot")
async def get_screenshot():
    if engine.screenshot_file.exists():
        return Response(content=engine.screenshot_file.read_bytes(), media_type="image/png")
    return Response(content=b"", status_code=404)


@app.get("/api/logs")
async def stream_logs():
    q: asyncio.Queue = asyncio.Queue()
    LOG_SUBSCRIBERS.append(q)

    async def event_generator():
        try:
            while True:
                line = await q.get()
                yield f"data: {line}\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            if q in LOG_SUBSCRIBERS:
                LOG_SUBSCRIBERS.remove(q)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("INGRESS_PORT", 8099))
    uvicorn.run(app, host="0.0.0.0", port=port)
