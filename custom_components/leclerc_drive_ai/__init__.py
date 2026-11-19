"""Intégration Home Assistant E.Leclerc Drive AI."""

from __future__ import annotations

import json
import logging
import re
import urllib.request
from pathlib import Path
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import (
    AI_DEFAULT_OLLAMA,
    CONF_CONVERSATION_ENTITY,
    CONF_GOOGLE_EMAIL,
    CONF_GOOGLE_MASTER_TOKEN,
    CONF_KEEP_NOTE_TITLE,
    CONF_LECLERC_EMAIL,
    CONF_LECLERC_PASSWORD,
    CONF_LIST_SOURCE,
    CONF_MAGASIN_SLUG,
    CONF_OLLAMA_MODEL,
    CONF_OLLAMA_URL,
    CONF_PLURAL_ITEMS_COUNT,
    CONF_SESSION_TOKEN,
    CONF_TODO_ENTITY_ID,
    DEFAULT_KEEP_NOTE_TITLE,
    DEFAULT_MAGASIN_SLUG,
    DEFAULT_OLLAMA_MODEL,
    DEFAULT_OLLAMA_URL,
    DEFAULT_PLURAL_COUNT,
    DOMAIN,
    PLATFORMS,
    SOURCE_GOOGLE_KEEP,
)
from .leclerc_worker import (
    GENERIC_WORDS,
    LeclercDriveWorker,
    _norm_text,
    _significant_tokens,
    fetch_google_keep_items,
)

_LOGGER = logging.getLogger(__name__)
SIGNAL_STATE_UPDATED = f"{DOMAIN}_state_updated"


def _is_generic_category(text: str) -> bool:
    norm_words = set(_norm_text(text).split())
    if bool(norm_words & GENERIC_WORDS):
        return True
    if not _significant_tokens(text, exclude_generic=True):
        return True
    return False


def _call_ollama_json(ollama_url: str, model: str, prompt: str) -> dict:
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "format": "json",
        "stream": False,
        "keep_alive": 0,
        "options": {"temperature": 0.1, "num_ctx": 3072},
    }
    req = urllib.request.Request(
        f"{ollama_url.rstrip('/')}/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=90) as resp:
        body = json.loads(resp.read().decode("utf-8"))
        content = body.get("message", {}).get("content", "{}")
        parsed = json.loads(content)
        return parsed if isinstance(parsed, dict) else {"plan": [], "doutes_ia": []}


class LeclercDriveCoordinator:
    """Coordonne la récupération Keep/Todo, l'appel IA (conversation.* ou qwen2.5:1.5b) et Playwright."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        storage_dir = Path(hass.config.path(".storage", DOMAIN))
        self.worker = LeclercDriveWorker(
            storage_dir=storage_dir,
            magasin_slug=entry.data.get(CONF_MAGASIN_SLUG, DEFAULT_MAGASIN_SLUG),
            plural_count=entry.data.get(CONF_PLURAL_ITEMS_COUNT, DEFAULT_PLURAL_COUNT),
        )
        stats = self.worker.compute_stats()
        self.avg_basket_eur: float = stats.get("avg_basket_eur", 0.0)
        self.orders_count: int = stats.get("orders_count", 0)
        self.cart_total_str: str = "0,00 €"
        self.cart_count: int = 0
        self.last_status: str = "Prêt"
        self.last_report: dict[str, Any] = {}

    async def async_fetch_shopping_items(self) -> list[str]:
        source = self.entry.data.get(CONF_LIST_SOURCE, SOURCE_GOOGLE_KEEP)
        if source == SOURCE_GOOGLE_KEEP:
            g_email = self.entry.data.get(CONF_GOOGLE_EMAIL, "")
            g_token = self.entry.data.get(CONF_GOOGLE_MASTER_TOKEN, "")
            note_title = self.entry.data.get(CONF_KEEP_NOTE_TITLE, DEFAULT_KEEP_NOTE_TITLE)
            return await self.hass.async_add_executor_job(
                fetch_google_keep_items, g_email, g_token, note_title
            )

        # Liste de courses native Home Assistant (todo.*)
        todo_entity = self.entry.data.get(CONF_TODO_ENTITY_ID, "todo.shopping_list")
        resp = await self.hass.services.async_call(
            "todo",
            "get_items",
            {"entity_id": todo_entity, "status": ["needs_action"]},
            blocking=True,
            return_response=True,
        )
        items_data = (resp or {}).get(todo_entity, {}).get("items", [])
        return [str(it.get("summary", "")).strip() for it in items_data if it.get("summary")]

    async def async_translate_with_ai(
        self,
        items_to_translate: list[str],
        all_keep_items: list[str],
        stats: dict,
        season_ctx: dict,
    ) -> tuple[list[dict], list[str], str]:
        """
        Utilise l'entité conversation.* choisie par l'utilisateur dans Home Assistant,
        ou par défaut Ollama local avec qwen2.5:1.5b (keep_alive=0).
        Fournit à l'IA la saison en France, la date et le contenu de la dernière commande,
        et récupère à la fois les traductions de produits et les doutes/remarques de l'IA.
        """
        top_products = [
            {
                "id": p.get("leclerc_id"),
                "name": p.get("name"),
                "achats": p.get("occurrences"),
                "dans_derniere_commande": p.get("in_last_order"),
                "qty_habituelle": p.get("suggested_qty", 1),
            }
            for p in stats.get("products", [])[:30]
        ]
        prompt = (
            "Tu es un assistant courses E.Leclerc Drive intelligent pour un foyer en France.\n"
            f"- Date & Saison actuelle en France : {season_ctx['date_str']} — {season_ctx['summary']}\n"
            f"- Légumes de saison en France ({season_ctx['month_name']}) : {', '.join(season_ctx['seasonal_veg'][:8])}\n"
            f"- Fruits de saison en France ({season_ctx['month_name']}) : {', '.join(season_ctx['seasonal_fruits'][:7])}\n"
            f"- Dernière commande : {stats.get('last_order_date')} (il y a {stats.get('days_since_last_order')}j, moyenne {stats.get('avg_days_between_orders')}j)\n"
            f"- Mode de courses détecté : {stats.get('shopping_mode_label')}\n"
            f"- Articles déjà achetés lors de la DERNIÈRE commande : {json.dumps(stats.get('last_order_items', [])[:15], ensure_ascii=False)}\n"
            f"- Liste complète demandée aujourd'hui : {json.dumps(all_keep_items, ensure_ascii=False)}\n"
            f"- Articles restant à traduire : {json.dumps(items_to_translate, ensure_ascii=False)}\n"
            f"- Historique du foyer : {json.dumps(top_products, ensure_ascii=False)}\n\n"
            "CONSIGNES :\n"
            "1. Traduis les articles de 'Articles restant à traduire' en produits précis (vide si la liste à traduire est vide).\n"
            "2. Ne cumule pas plusieurs gros packs surgelés de viande si la dernière commande en contenait déjà.\n"
            "3. Pour les fruits/légumes de saison, respecte la saison actuelle en France.\n"
            "4. Dans 'doutes_ia', indique 1 à 3 remarques courtes et pertinentes en français (doutes sur un article, "
            "risque de doublon avec la dernière commande, ou conseil saisonnier).\n\n"
            "Réponds UNIQUEMENT en JSON : "
            '{"plan": [{"keep_item": "...", "search_query": "...", "preferred_leclerc_id": "...", "qty": 1}], '
            '"doutes_ia": ["..."]}'
        )

        conv_entity = self.entry.data.get(CONF_CONVERSATION_ENTITY)
        if conv_entity and conv_entity != AI_DEFAULT_OLLAMA:
            try:
                resp = await self.hass.services.async_call(
                    "conversation",
                    "process",
                    {"agent_id": conv_entity, "text": prompt},
                    blocking=True,
                    return_response=True,
                )
                speech = (
                    (resp or {})
                    .get("response", {})
                    .get("speech", {})
                    .get("plain", {})
                    .get("speech", "")
                )
                m = re.search(r"\{.*\}", speech, re.DOTALL)
                if m:
                    parsed = json.loads(m.group(0))
                    plan = parsed.get("plan") or parsed.get("items") or []
                    doutes = [str(d).strip() for d in (parsed.get("doutes_ia") or []) if d and str(d).strip()]
                    return plan, doutes, f"Home Assistant ({conv_entity})"
            except Exception as err:
                _LOGGER.warning("Erreur sur l'entité %s (%s), repli sur Ollama qwen2.5:1.5b", conv_entity, err)

        ollama_url = self.entry.data.get(CONF_OLLAMA_URL, DEFAULT_OLLAMA_URL)
        ollama_model = self.entry.data.get(CONF_OLLAMA_MODEL, DEFAULT_OLLAMA_MODEL)
        try:
            parsed = await self.hass.async_add_executor_job(
                _call_ollama_json, ollama_url, ollama_model, prompt
            )
            plan = parsed.get("plan") or parsed.get("items") or []
            doutes = [str(d).strip() for d in (parsed.get("doutes_ia") or []) if d and str(d).strip()]
            return plan, doutes, f"Ollama ({ollama_model})"
        except Exception as err:
            _LOGGER.warning("Ollama indisponible (%s), repli 100%% Python pur", err)
            return [], [], "Python pur (sans LLM)"

    async def async_sync_history(self) -> None:
        self.last_status = "Synchronisation de l'historique en cours..."
        async_dispatcher_send(self.hass, SIGNAL_STATE_UPDATED)

        def _job():
            self.worker.login_leclerc(
                self.entry.data[CONF_LECLERC_EMAIL],
                self.entry.data[CONF_LECLERC_PASSWORD],
                self.entry.data.get(CONF_SESSION_TOKEN, ""),
            )
            return self.worker.auto_scrape_order_history(max_orders=15)

        res = await self.hass.async_add_executor_job(_job)
        stats = await self.hass.async_add_executor_job(self.worker.compute_stats)
        self.avg_basket_eur = stats.get("avg_basket_eur", 0.0)
        self.orders_count = stats.get("orders_count", 0)
        self.last_status = f"Historique synchronisé ({self.orders_count} commandes)"
        self.last_report = res
        async_dispatcher_send(self.hass, SIGNAL_STATE_UPDATED)

    async def async_fill_cart(self) -> None:
        self.last_status = "Lecture de la liste de courses..."
        async_dispatcher_send(self.hass, SIGNAL_STATE_UPDATED)

        keep_items = await self.async_fetch_shopping_items()
        if not keep_items:
            self.last_status = "Liste de courses vide"
            async_dispatcher_send(self.hass, SIGNAL_STATE_UPDATED)
            return

        stats = await self.hass.async_add_executor_job(self.worker.compute_stats, 300)
        season_ctx = self.worker.get_french_season_context()
        products = stats.get("products", [])
        self.avg_basket_eur = stats.get("avg_basket_eur", 0.0)
        self.orders_count = stats.get("orders_count", 0)

        shopping_plan: list[dict] = []
        items_needing_ai: list[str] = []
        arbitrations: list[str] = []

        for raw_item in keep_items:
            if _is_generic_category(raw_item):
                family_prods = self.worker.find_semantic_family_products(
                    raw_item, products, stats=stats, arbitrations_out=arbitrations
                )
                if family_prods:
                    for fp in family_prods:
                        short_q = fp["name"].split(" - ")[0].strip()
                        qty = max(1, int(fp.get("suggested_qty", 1) or 1))
                        if stats.get("shopping_mode") == "giga_courses_frigo_a_sec" and fp.get("occurrences", 0) >= 6:
                            qty = max(qty, min(3, qty + 1))
                        shopping_plan.append({
                            "keep_item": raw_item,
                            "search_query": short_q if len(short_q) >= 4 else fp["name"],
                            "preferred_leclerc_id": fp.get("leclerc_id"),
                            "qty": qty,
                        })
                else:
                    items_needing_ai.append(raw_item)
            else:
                match = self.worker.find_history_match(raw_item, products)
                if match:
                    short_q = match["name"].split(" - ")[0].strip()
                    if match.get("in_last_order") and (stats.get("days_since_last_order") or 99) <= 7:
                        arbitrations.append(
                            f"🤔 Doute sur '{raw_item}' : '{short_q}' était déjà dans la dernière commande ({stats.get('last_order_date')}), ajouté car demandé explicitement."
                        )
                    shopping_plan.append({
                        "keep_item": raw_item,
                        "search_query": short_q if len(short_q) >= 4 else match["name"],
                        "preferred_leclerc_id": match.get("leclerc_id"),
                        "qty": max(1, int(match.get("suggested_qty", 1) or 1)),
                    })
                else:
                    # Produit précis mais jamais acheté (ex: 'Levure seche') -> recherche directe sans substitution
                    arbitrations.append(
                        f"🤔 Article inédit '{raw_item}' : absent de l'historique ({self.orders_count} commandes), recherche directe sans substitution."
                    )
                    shopping_plan.append({
                        "keep_item": raw_item,
                        "search_query": raw_item,
                        "preferred_leclerc_id": None,
                        "qty": 1,
                    })

        self.last_status = "Analyse IA (saisonnalité, dernière commande & doutes)..."
        async_dispatcher_send(self.hass, SIGNAL_STATE_UPDATED)
        ai_plan, ai_doubts, ai_used = await self.async_translate_with_ai(
            items_needing_ai, keep_items, stats, season_ctx
        )
        for d in ai_doubts:
            if d not in arbitrations:
                arbitrations.append(f"🤖 Audit IA : {d}")

        if items_needing_ai:
            for entry in ai_plan:
                q = str(entry.get("search_query", "")).strip()
                if q:
                    shopping_plan.append({
                        "keep_item": entry.get("keep_item", ""),
                        "search_query": q,
                        "preferred_leclerc_id": entry.get("preferred_leclerc_id"),
                        "qty": max(1, int(entry.get("qty", 1) or 1)),
                    })

        reminders = self.worker.detect_forgotten_staples(shopping_plan, stats)

        self.last_status = f"Remplissage du panier ({len(shopping_plan)} produits)..."
        async_dispatcher_send(self.hass, SIGNAL_STATE_UPDATED)

        def _execute_playwright():
            added = []
            missing = []
            try:
                self.worker.login_leclerc(
                    self.entry.data[CONF_LECLERC_EMAIL],
                    self.entry.data[CONF_LECLERC_PASSWORD],
                    self.entry.data.get(CONF_SESSION_TOKEN, ""),
                )
                for step in shopping_plan:
                    res = self.worker.add_product_by_query(
                        query=step["search_query"],
                        qty=step["qty"],
                        preferred_leclerc_id=step["preferred_leclerc_id"],
                    )
                    if res.get("status") == "added":
                        added.append({
                            "item": step["keep_item"],
                            "product": res.get("selected_product"),
                            "price": res.get("price"),
                            "qty": res.get("qty"),
                        })
                        self.cart_total_str = res.get("cart_total_after", self.cart_total_str)
                        self.cart_count = int(res.get("cart_count_after", self.cart_count))
                    else:
                        missing.append({
                            "item": step["keep_item"],
                            "query": step["search_query"],
                            "reason": res.get("reason", "Introuvable sur le Drive — aucune substitution effectuée"),
                        })
            finally:
                self.worker.close()
            return {
                "saison_france": season_ctx["summary"],
                "derniere_commande": stats.get("last_order_date"),
                "jours_depuis_derniere_commande": stats.get("days_since_last_order"),
                "mode_courses": stats.get("shopping_mode_label"),
                "added": added,
                "missing": missing,
                "doutes_et_arbitrages": arbitrations,
                "rappels_oublis_habituels": reminders,
                "ai_used": ai_used,
            }

        report = await self.hass.async_add_executor_job(_execute_playwright)
        self.last_report = report
        self.last_status = (
            f"Terminé : {len(report['added'])} ajouté(s), {len(report['missing'])} manquant(s), "
            f"{len(report['rappels_oublis_habituels'])} rappel(s)"
        )
        async_dispatcher_send(self.hass, SIGNAL_STATE_UPDATED)

        # Notification persistante détaillée dans Home Assistant
        msg_lines = [
            f"**Panier E.Leclerc actuel :** {self.cart_count} article(s) ({self.cart_total_str}) — *Moyenne historique : {self.avg_basket_eur} €*",
            f"**Saison (France) :** {season_ctx['summary']}",
            f"**Rythme détecté :** {stats.get('shopping_mode_label')}",
            f"**Moteur IA utilisé :** {ai_used}",
            "",
            f"### ✅ Produits ajoutés au panier ({len(report['added'])}) :",
        ]
        if report["added"]:
            for a in report["added"]:
                msg_lines.append(f"- **{a['item']}** → {a['product']} (x{a['qty']} — {a['price']})")
        else:
            msg_lines.append("- *(Aucun produit ajouté)*")

        if report["missing"]:
            msg_lines.append(f"\n### ❌ Produits manquants / introuvables ({len(report['missing'])}) :")
            for m in report["missing"]:
                msg_lines.append(f"- **{m['item']}** (*recherche : {m['query']}*) : {m['reason']}")

        if report["doutes_et_arbitrages"]:
            msg_lines.append(f"\n### 🤔 Doutes & Arbitrages de l'IA ({len(report['doutes_et_arbitrages'])}) :")
            for d in report["doutes_et_arbitrages"]:
                msg_lines.append(f"- {d}")

        if report["rappels_oublis_habituels"]:
            msg_lines.append(f"\n### 🔔 Rappels : Produits habituels oubliés aujourd'hui ({len(report['rappels_oublis_habituels'])}) :")
            for r in report["rappels_oublis_habituels"]:
                msg_lines.append(f"- **{r['name']}** — *{r['reason']}*")

        await self.hass.services.async_call(
            "persistent_notification",
            "create",
            {
                "title": "🛒 Rapport Courses E.Leclerc Drive IA",
                "message": "\n".join(msg_lines),
                "notification_id": f"{DOMAIN}_cart_report",
            },
        )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator = LeclercDriveCoordinator(hass, entry)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    async def _handle_fill_cart(_call: ServiceCall) -> None:
        await coordinator.async_fill_cart()

    async def _handle_sync_history(_call: ServiceCall) -> None:
        await coordinator.async_sync_history()

    hass.services.async_register(DOMAIN, "fill_cart", _handle_fill_cart)
    hass.services.async_register(DOMAIN, "sync_history", _handle_sync_history)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok
