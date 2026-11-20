"""
Moteur d'automatisation E.Leclerc Drive pour l'Add-on Home Assistant.
Intègre :
1. Rotation intelligente des viandes par rapport à la dernière commande (anti-saturation congélateur).
2. Saisonnalité des fruits et légumes en France.
3. Détection du mode de courses (Appoint, Normal, Giga courses / Frigo à sec).
4. Détection des produits habituels oubliés (rappels).
5. Recherche stricte par mots-clés (aucune substitution hasardeuse).
6. Cadencement humain anti-bridage Drive.
7. Support multi-source de liste (Google Keep ou Todo HA) et IA (Ollama ou conversation.* HA).
"""

from __future__ import annotations

import base64
import json
import logging
import os
import random
import re
import shutil
import time
import unicodedata
import urllib.request
import zlib
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote_plus

_LOGGER = logging.getLogger("leclerc_engine")

CHROME_CANDIDATES = (
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/google-chrome",
)

CHROME_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--headless=new",
    "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
]

GENERIC_WORDS = {
    "legume", "legumes", "fruit", "fruits", "viande", "viandes",
    "poisson", "poissons", "fromage", "fromages", "yaourt", "yaourts",
    "charcuterie", "charcuteries", "boisson", "boissons", "gouter", "gouters", "apero", "aperitif",
}

PLURAL_GENERIC_WORDS = {
    "legumes", "fruits", "viandes", "poissons", "fromages",
    "yaourts", "charcuteries", "boissons", "gouters",
}


def norm_text(text: str) -> str:
    replaced = (text or "").lower().replace("œ", "oe").replace("æ", "ae")
    nfkd = unicodedata.normalize("NFKD", replaced)
    no_accents = "".join(c for c in nfkd if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", no_accents).strip()


def significant_tokens(text: str, exclude_generic: bool = False) -> list[str]:
    stop = {
        "de", "du", "des", "la", "le", "les", "au", "aux", "en", "et", "ou",
        "un", "une", "pour", "avec", "sans", "frais", "fraiche", "saison", "bio",
    }
    tokens = []
    for tok in norm_text(text).split():
        if len(tok) <= 2 or tok in stop:
            continue
        if exclude_generic and tok in GENERIC_WORDS:
            continue
        stem = tok[:-1] if (len(tok) > 4 and tok.endswith(("s", "x"))) else tok
        tokens.append(stem)
    return tokens


def candidate_token_hits(cand_title: str, query_tokens: list[str]) -> tuple[bool, int]:
    if not query_tokens:
        return False, 0
    cand_words = [
        (w[:-1] if (len(w) > 4 and w.endswith(("s", "x"))) else w)
        for w in norm_text(cand_title).split()
        if len(w) >= 3
    ]
    if not cand_words:
        return False, 0

    def word_matches(qt: str) -> bool:
        return any(cw == qt or cw.startswith(qt) or qt.startswith(cw) for cw in cand_words)

    if not word_matches(query_tokens[0]):
        return False, 0

    hits = sum(1 for qt in query_tokens if word_matches(qt))
    if len(query_tokens) == 2 and hits < 2:
        return False, hits

    min_required = len(query_tokens) if len(query_tokens) <= 2 else max(2, (len(query_tokens) + 1) // 2)
    return (hits >= min_required), hits


def product_subtype(norm_name: str) -> str:
    subtypes = [
        ("steak_hache", ("steak", "viande hachee")),
        ("cordon_bleu", ("cordon bleu", "cordons bleus")),
        ("filet_poulet", ("filet de poulet", "filets de poulet", "aiguillette")),
        ("blanc_poulet", ("blanc de poulet",)),
        ("boulette", ("boulette",)),
        ("kebab", ("kebab",)),
        ("nugget", ("nugget",)),
        ("saucisse", ("chipolata", "merguez", "knack")),
        ("emince_porc", ("emince",)),
        ("jambon", ("jambon",)),
        ("lardon", ("lardon",)),
        ("saucisson", ("saucisson",)),
        ("champignon", ("champignon",)),
        ("concombre", ("concombre",)),
        ("tomate", ("tomate",)),
        ("courgette", ("courgette",)),
        ("salade", ("salade", "laitue", "feuille de chene", "roquette", "endive")),
        ("chou", ("chou",)),
        ("brocoli", ("brocoli",)),
        ("poireau", ("poireau",)),
        ("carotte", ("carotte",)),
        ("pdt", ("pomme de terre", "pommes de terre")),
        ("pomme", ("pomme", "pommes")),
        ("poire", ("poire", "poires")),
        ("abricot", ("abricot", "abricots")),
        ("nectarine", ("nectarine", "peche")),
        ("kiwi", ("kiwi",)),
        ("banane", ("banane",)),
        ("raisin", ("raisin",)),
        ("mandarine", ("mandarine", "clementine")),
        ("yaourt_fruit", ("veloute", "fruix", "aux fruits")),
        ("yaourt_nature", ("yaourt nature",)),
        ("parmesan", ("parmigiano", "parmesan")),
        ("chaource", ("chaource",)),
        ("boursin", ("boursin",)),
        ("babybel", ("babybel",)),
    ]
    for key, patterns in subtypes:
        if any(pat in norm_name for pat in patterns):
            return key
    words = [w for w in norm_name.split() if len(w) > 2]
    return "_".join(words[:2]) if words else norm_name


def parse_store_input(raw_input: str) -> tuple[str, str | None, str | None]:
    cleaned = (raw_input or "").strip()
    shard_match = re.search(r"(fd\d+)-(?:courses|espace-client)\.leclercdrive\.fr", cleaned, re.IGNORECASE)
    shard = shard_match.group(1).lower() if shard_match else "fd2"

    slug_match = re.search(r"(magasin-\d+-(?:\d+-)?[A-Za-z0-9_-]+)", cleaned)
    if slug_match:
        slug = slug_match.group(1).rstrip("/")
        if slug.endswith(".aspx"):
            slug = slug[:-5]
        return shard, slug, None

    return shard, None, (cleaned if cleaned else None)


def find_chrome_executable() -> str | None:
    for candidate in CHROME_CANDIDATES:
        if Path(candidate).exists():
            return candidate
        which_path = shutil.which(Path(candidate).name)
        if which_path:
            return which_path
    return None


def extract_google_token_from_string(raw: str) -> str:
    cleaned = (raw or "").strip()
    import urllib.parse
    decoded = urllib.parse.unquote(cleaned)

    m_master = re.search(r"(aas_et/[a-zA-Z0-9_\-\.]+)", decoded)
    if m_master:
        return m_master.group(1)

    m_rt = re.search(r"(oauth2rt_1/[a-zA-Z0-9_\-\.]+)", decoded)
    if m_rt:
        return m_rt.group(1)

    m_oauth2 = re.search(r"(oauth2_4/[a-zA-Z0-9_\-\.]+)", decoded)
    if m_oauth2:
        return m_oauth2.group(1)

    m_kv = re.search(r"oauth_token=([a-zA-Z0-9_\-\./]+)", decoded)
    if m_kv:
        return m_kv.group(1)

    return decoded


def resolve_google_keep_master_token(google_email: str, secret_or_token: str) -> str:
    token = extract_google_token_from_string(secret_or_token)

    if token.startswith(("aas_et/", "oauth2rt_1/")):
        return token

    import gpsoauth
    android_id = "0123456789abcdef"

    if token.startswith("oauth2_4/"):
        res = gpsoauth.exchange_token(google_email.strip(), token, android_id)
        if "Token" in res:
            return res["Token"]
        err_msg = res.get("Error", res)
        raise ValueError(f"Échec de l'échange du token Google EmbeddedSetup ({err_msg}). Vérifiez votre adresse Gmail.")

    app_pwd = token.replace(" ", "")
    if len(app_pwd) == 16 and app_pwd.isalpha():
        res = gpsoauth.perform_master_login(google_email.strip(), app_pwd, android_id)
        if "Token" in res:
            return res["Token"]
        raise ValueError(
            "Google a refusé le mot de passe d'application (BadAuthentication). "
            "Google bloque désormais les mots de passe d'application sur cette API. "
            "Récupérez votre jeton 'oauth2_4/...' via https://accounts.google.com/EmbeddedSetup."
        )

    raise ValueError("Format de token non reconnu. Le jeton Google Keep doit commencer par 'oauth2_4/' ou 'aas_et/'.")


def fetch_google_keep_items(google_email: str, master_token: str, note_title: str = "Liste de courses") -> list[str]:
    import gkeepapi
    keep = gkeepapi.Keep()
    keep.authenticate(google_email.strip(), master_token.strip())
    glists = keep.find(query=note_title)
    target_list = next(glists, None)
    if not target_list:
        return []
    return [
        item.text.strip()
        for item in target_list.items
        if not item.checked and item.text.strip()
    ]


class LeclercEngine:
    def __init__(self, data_dir: Path, log_callback: Callable[[str], None] | None = None) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.session_dir = self.data_dir / "browser_session"
        self.history_file = self.data_dir / "history.json"
        self.cookies_file = self.data_dir / "imported_cookies.json"
        self.screenshot_file = self.data_dir / "last_screenshot.png"
        self.log_callback = log_callback or (lambda msg: _LOGGER.info(msg))

        self.fd_shard = "fd2"
        self.magasin_slug = ""
        self.store_search_hint = None
        self.plural_count = 3

        self._playwright = None
        self._context = None
        self._page = None

    def log(self, message: str) -> None:
        try:
            self.log_callback(message)
        except Exception:
            pass
        _LOGGER.info(message)

    def set_store(self, store_input: str) -> None:
        shard, slug, hint = parse_store_input(store_input)
        self.fd_shard = shard
        self.magasin_slug = slug or ""
        self.store_search_hint = hint

    def load_history(self) -> dict:
        if not self.history_file.exists():
            return {"last_updated": None, "orders": []}
        try:
            return json.loads(self.history_file.read_text(encoding="utf-8"))
        except Exception:
            return {"last_updated": None, "orders": []}

    def save_history(self, data: dict) -> None:
        data["last_updated"] = datetime.now().strftime("%Y-%m-%d")
        self.history_file.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    def compute_stats(self, max_products: int = 300) -> dict:
        data = self.load_history()
        orders = data.get("orders", [])
        if not orders:
            return {
                "orders_count": 0,
                "avg_basket_eur": 0.0,
                "avg_days_between_orders": 14.0,
                "days_since_last_order": 14,
                "shopping_mode": "courses_normales",
                "shopping_mode_label": "Aucun historique disponible",
                "products": [],
            }

        sorted_orders = sorted(orders, key=lambda o: o.get("date", ""))
        totals = [float(o.get("total_eur", 0.0)) for o in sorted_orders if o.get("total_eur") is not None]
        avg_basket = round(sum(totals) / len(totals), 2) if totals else 0.0

        today = datetime.now().date()
        order_dates = []
        for o in sorted_orders:
            d_str = o.get("date")
            if d_str:
                try:
                    order_dates.append(datetime.strptime(d_str, "%Y-%m-%d").date())
                except ValueError:
                    pass

        order_intervals = [
            (order_dates[i] - order_dates[i - 1]).days
            for i in range(1, len(order_dates))
            if (order_dates[i] - order_dates[i - 1]).days > 0
        ]
        avg_days = round(sum(order_intervals) / len(order_intervals), 1) if order_intervals else 14.0
        last_date = sorted_orders[-1].get("date") if sorted_orders else None
        days_since = (today - order_dates[-1]).days if order_dates else 14

        if days_since <= 5:
            mode = "appoint_recent"
            mode_lbl = f"Courses rapprochées (dernière commande il y a {days_since}j le {last_date} -> quantités modérées, éviter les doublons)"
        elif days_since >= max(18, int(avg_days * 1.25)):
            mode = "giga_courses_frigo_a_sec"
            mode_lbl = f"Giga courses / Frigo à sec (dernière commande il y a {days_since}j le {last_date}, moyenne {avg_days}j -> rechargement complet)"
        else:
            mode = "courses_normales"
            mode_lbl = f"Courses régulières normales (dernière commande il y a {days_since}j le {last_date}, moyenne {avg_days}j)"

        last_items = sorted_orders[-1].get("items", []) if sorted_orders else []
        last_keys = {str(it.get("leclerc_id") or it.get("name", "")).strip() for it in last_items}

        product_history: dict[str, dict] = {}
        for order in sorted_orders:
            d_str = order.get("date")
            try:
                od = datetime.strptime(d_str, "%Y-%m-%d").date() if d_str else None
            except ValueError:
                od = None

            seen_in_order = set()
            for it in order.get("items", []):
                key = str(it.get("leclerc_id") or it.get("name", "")).strip()
                if not key:
                    continue
                entry = product_history.setdefault(
                    key,
                    {
                        "leclerc_id": it.get("leclerc_id"),
                        "name": it.get("name", ""),
                        "category": it.get("category"),
                        "occurrences": 0,
                        "quantities": [],
                        "unit_prices": [],
                        "dates": [],
                    },
                )
                q = int(it.get("qty", 1) or 1)
                p = float(it.get("unit_price", it.get("line_total_eur", 0.0)) or 0.0)
                entry["quantities"].append(q)
                if p > 0:
                    entry["unit_prices"].append(p)
                if key not in seen_in_order:
                    entry["occurrences"] += 1
                    seen_in_order.add(key)
                    if od:
                        entry["dates"].append(od)

        summary = []
        for key, entry in product_history.items():
            dates = sorted(entry["dates"])
            intervals = [(dates[i] - dates[i - 1]).days for i in range(1, len(dates)) if (dates[i] - dates[i - 1]).days > 0]
            avg_int = round(sum(intervals) / len(intervals), 1) if intervals else None
            days_last = (today - dates[-1]).days if dates else None
            suggested_qty = max(1, round(sum(entry["quantities"]) / len(entry["quantities"]))) if entry["quantities"] else 1
            due = bool(entry["occurrences"] >= 3 and avg_int and days_last and days_last >= max(7, avg_int * 0.85))

            summary.append({
                "leclerc_id": entry["leclerc_id"],
                "name": entry["name"],
                "category": entry["category"],
                "occurrences": entry["occurrences"],
                "avg_interval_days": avg_int,
                "days_since_last": days_last,
                "in_last_order": key in last_keys,
                "due_for_restock": due,
                "suggested_qty": suggested_qty,
                "unit_price": round(sum(entry["unit_prices"]) / len(entry["unit_prices"]), 2) if entry["unit_prices"] else 0.0,
            })

        summary.sort(key=lambda p: (-(p["occurrences"]), -(p["days_since_last"] or 0)))
        return {
            "orders_count": len(sorted_orders),
            "avg_basket_eur": avg_basket,
            "avg_days_between_orders": avg_days,
            "last_order_date": last_date,
            "days_since_last_order": days_since,
            "shopping_mode": mode,
            "shopping_mode_label": mode_lbl,
            "last_order_items": [it.get("name", "") for it in last_items if it.get("name")],
            "products": summary[:max_products],
        }

    def get_french_season_context(self) -> dict:
        months = {
            1: "Janvier", 2: "Février", 3: "Mars", 4: "Avril",
            5: "Mai", 6: "Juin", 7: "Juillet", 8: "Août",
            9: "Septembre", 10: "Octobre", 11: "Novembre", 12: "Décembre",
        }
        now = datetime.now()
        m = now.month
        month_name = months.get(m, "Novembre")

        if m in (12, 1, 2):
            season = "Hiver"
            s_veg = ("champignon", "poireau", "chou", "brocoli", "endive", "carotte", "butternut", "courge", "epinard", "salade", "laitue", "mache", "pomme de terre")
            oos_veg = ("tomate", "courgette", "concombre", "aubergine", "poivron")
            s_fruits = ("pomme", "poire", "kiwi", "mandarine", "clementine", "orange", "citron", "banane")
            oos_fruits = ("abricot", "nectarine", "peche", "fraise", "cerise", "melon", "myrtille", "prune", "raisin")
        elif m in (3, 4, 5):
            season = "Printemps"
            s_veg = ("asperge", "radis", "epinard", "salade", "laitue", "carotte", "champignon", "chou", "poireau", "brocoli", "pomme de terre")
            oos_veg = ("butternut", "courge", "tomate", "aubergine", "poivron")
            s_fruits = ("fraise", "pomme", "poire", "kiwi", "banane", "citron")
            oos_fruits = ("abricot", "nectarine", "peche", "melon", "raisin", "prune", "mandarine")
        elif m in (6, 7, 8):
            season = "Été"
            s_veg = ("tomate", "courgette", "concombre", "aubergine", "poivron", "haricot", "salade", "laitue", "roquette", "feuille de chene")
            oos_veg = ("endive", "butternut", "courge", "chou chinois", "poireau")
            s_fruits = ("abricot", "nectarine", "peche", "melon", "fraise", "cerise", "myrtille", "prune", "framboise")
            oos_fruits = ("mandarine", "clementine", "kiwi")
        else:
            season = "Automne"
            s_veg = ("champignon", "brocoli", "chou", "poireau", "carotte", "butternut", "courge", "epinard", "salade", "laitue", "feuille de chene", "endive", "pomme de terre")
            oos_veg = ("tomate", "concombre", "courgette", "aubergine", "poivron")
            s_fruits = ("pomme", "poire", "kiwi", "raisin", "mandarine", "clementine", "prune", "banane", "citron")
            oos_fruits = ("abricot", "nectarine", "peche", "fraise", "cerise", "melon")

        return {
            "date_str": now.strftime("%Y-%m-%d"),
            "month_name": month_name,
            "season": season,
            "seasonal_veg": s_veg,
            "out_of_season_veg": oos_veg,
            "seasonal_fruits": s_fruits,
            "out_of_season_fruits": oos_fruits,
            "summary": f"{season} ({month_name} en France)",
        }

    def find_history_match(self, keep_item: str, products: list[dict]) -> dict | None:
        tokens = significant_tokens(keep_item, exclude_generic=True)
        if not tokens:
            return None
        best = None
        best_score = 0
        for p in products:
            p_words = set(norm_text(p.get("name", "")).split())
            hits = sum(1 for t in tokens if any(w.startswith(t) or t.startswith(w) for w in p_words if len(w) >= 3))
            if hits == 0 or (len(tokens) <= 2 and hits < len(tokens)):
                continue
            score = hits * 100 + int(p.get("occurrences", 0)) * 5
            if score > best_score:
                best_score = score
                best = p
        return best

    def find_semantic_family_products(
        self,
        generic_item: str,
        products: list[dict],
        stats: dict | None = None,
        arbitrations_out: list[str] | None = None,
    ) -> list[dict]:
        stats = stats or {}
        mode = stats.get("shopping_mode", "courses_normales")
        last_date = stats.get("last_order_date", "récente")
        season_ctx = self.get_french_season_context()

        m = re.search(r"\b(\d+)\b", generic_item)
        if m:
            count = max(1, min(6, int(m.group(1))))
        elif bool(set(norm_text(generic_item).split()) & PLURAL_GENERIC_WORDS):
            count = 2 if mode == "appoint_recent" else (max(3, self.plural_count + 1) if mode == "giga_courses_frigo_a_sec" else self.plural_count)
        else:
            count = 1

        norm_item = norm_text(generic_item)
        pool = []
        skipped_seasonal = []
        skipped_last = []

        for p in products:
            pname = norm_text(p.get("name", ""))
            pcat = norm_text(p.get("category") or "")

            if "viande" in norm_item:
                is_meat = any(w in pname for w in (
                    "steak", "viande", "boeuf", "bovine", "cordon bleu", "cordons bleus",
                    "filet de poulet", "filets de poulet", "aiguillette", "blanc de poulet",
                    "boulette", "chipolata", "merguez", "kebab", "nugget", "emince de porc", "dinde",
                ))
                is_bad = any(bad in pname for bad in ("chips", "saveur", "gout", "pizza", "gnocchi", "tortellini", "ravioli", "sauce", "grisons"))
                if is_meat and not is_bad:
                    pool.append(p)
            elif "legume" in norm_item:
                is_fruit_bad = any(bad in pname for bad in (
                    "jus", "brique", "nectar", "sirop", "abricot", "nectarine", "peche",
                    "poire", "kiwi", "banane", "raisin", "fraise", "cerise", "melon",
                    "myrtille", "prune", "mandarine", "citron", "mangue", "pink lady", "pommes bicolores",
                ))
                is_veg = ("fruits legumes" in pcat or "surgele" in pcat) and not is_fruit_bad
                if is_veg and "fromage blanc" not in pname:
                    if any(oos in pname for oos in season_ctx["out_of_season_veg"]):
                        skipped_seasonal.append(p["name"].split(" - ")[0])
                        continue
                    pool.append(p)
            elif "fruit" in norm_item:
                if "fruits legumes" in pcat and any(f in pname for f in ("pomme", "poire", "banane", "kiwi", "abricot", "nectarine", "peche", "raisin", "fraise", "cerise", "melon", "mandarine", "citron")) and "pomme de terre" not in pname:
                    if any(oos in pname for oos in season_ctx["out_of_season_fruits"]):
                        skipped_seasonal.append(p["name"].split(" - ")[0])
                        continue
                    pool.append(p)
            elif "fromage" in norm_item:
                if any(w in pname for w in ("fromage", "chaource", "parmigiano", "parmesan", "boursin", "babybel", "emmental", "comte", "chevre", "mozzarella", "camembert")):
                    pool.append(p)
            elif "yaourt" in norm_item:
                if any(w in pname for w in ("yaourt", "veloute", "yoplait", "danone", "activia")):
                    pool.append(p)

        if skipped_seasonal and arbitrations_out is not None:
            uniq_s = list(dict.fromkeys(skipped_seasonal))[:4]
            arbitrations_out.append(f"🍂 Saisonnalité ({season_ctx['summary']}) pour '{generic_item}' : hors-saison écartés ({', '.join(uniq_s)}).")

        subtype_totals: dict[str, int] = {}
        for p in pool:
            st = product_subtype(norm_text(p.get("name", "")))
            subtype_totals[st] = subtype_totals.get(st, 0) + int(p.get("occurrences", 0))

        def smart_key(prod: dict):
            in_last = bool(prod.get("in_last_order"))
            pname = norm_text(prod.get("name", ""))
            st = product_subtype(pname)
            in_s = 1 if any(s in pname for s in (season_ctx["seasonal_veg"] + season_ctx["seasonal_fruits"])) else 0
            return (0 if in_last else 1, in_s, subtype_totals.get(st, 0), int(prod.get("occurrences", 0)))

        pool.sort(key=smart_key, reverse=True)

        max_freezer = 2 if mode == "giga_courses_frigo_a_sec" else 1
        freezer_count = 0
        selected = []
        seen_subtypes = set()

        for p in pool:
            pname = norm_text(p.get("name", ""))
            pcat = norm_text(p.get("category") or "")
            st = product_subtype(pname)
            if st in seen_subtypes:
                continue
            if p.get("in_last_order") and len(pool) > count:
                skipped_last.append(p["name"].split(" - ")[0])
                continue

            is_big_freezer = "surgele" in pcat or any(k in pname for k in ("1kg", "600g", "x10", "x50", "cordon bleu", "kebab", "nugget"))
            if "viande" in norm_item and is_big_freezer:
                if freezer_count >= max_freezer:
                    continue
                freezer_count += 1

            seen_subtypes.add(st)
            selected.append(p)
            if len(selected) >= count:
                break

        if len(selected) < count:
            for p in pool:
                st = product_subtype(norm_text(p.get("name", "")))
                if st not in seen_subtypes:
                    seen_subtypes.add(st)
                    selected.append(p)
                    if len(selected) >= count:
                        break

        if skipped_last and arbitrations_out is not None:
            uniq_l = list(dict.fromkeys(skipped_last))[:3]
            arbitrations_out.append(f"🔄 Rotation sur '{generic_item}' : {', '.join(uniq_l)} déjà acheté(s) à la dernière commande ({last_date}), écarté(s) pour varier.")

        return selected

    def detect_forgotten_staples(self, shopping_plan: list[dict], stats: dict, max_reminders: int = 6) -> list[dict]:
        products = stats.get("products", [])
        orders_count = max(1, int(stats.get("orders_count", 1)))
        planned_ids = {str(s.get("preferred_leclerc_id") or "").strip() for s in shopping_plan if s.get("preferred_leclerc_id")}
        planned_subtypes = {product_subtype(norm_text(s.get("search_query", ""))) for s in shopping_plan}
        planned_queries_norm = [norm_text(s.get("search_query", "")) for s in shopping_plan]

        reminders = []
        seen_subtypes = set()

        for p in products:
            lid = str(p.get("leclerc_id") or "").strip()
            pname = p.get("name", "")
            pnorm = norm_text(pname)
            st = product_subtype(pnorm)
            occ = int(p.get("occurrences", 0))
            days_last = p.get("days_since_last")
            avg_int = p.get("avg_interval_days")

            if (lid and lid in planned_ids) or st in planned_subtypes or st in seen_subtypes:
                continue
            if any(q and (q in pnorm or pnorm in q) for q in planned_queries_norm):
                continue
            if p.get("in_last_order") and (days_last is not None and days_last < 10):
                continue

            is_pillar = occ >= max(5, int(orders_count * 0.22))
            is_overdue = bool(p.get("due_for_restock") and occ >= 4)

            if is_pillar or is_overdue:
                seen_subtypes.add(st)
                pct = round((occ / orders_count) * 100)
                if avg_int and days_last is not None and days_last >= avg_int:
                    reason = f"Acheté dans {occ}/{orders_count} commandes ({pct}%), cycle habituel de ~{avg_int}j dépassé (dernier achat il y a {days_last}j)"
                else:
                    reason = f"Produit pilier acheté dans {occ}/{orders_count} commandes ({pct}%), dernier achat il y a {days_last}j"

                reminders.append({
                    "leclerc_id": lid,
                    "name": pname,
                    "occurrences": occ,
                    "days_since_last": days_last,
                    "reason": reason,
                })
                if len(reminders) >= max_reminders:
                    break
        return reminders

    def import_session_cookies(self, token_or_curl: str) -> int:
        cleaned = (token_or_curl or "").strip()
        if not cleaned:
            return 0
        try:
            cookies: list[dict] = []
            if cleaned.startswith("LECLERC_HA_V1:"):
                b64_part = cleaned.split("LECLERC_HA_V1:", 1)[1].strip()
                raw_json = zlib.decompress(base64.urlsafe_b64decode(b64_part)).decode("utf-8")
                cookies = json.loads(raw_json)
            elif cleaned.startswith("[") and cleaned.endswith("]"):
                raw_list = json.loads(cleaned)
                for c in raw_list:
                    if isinstance(c, dict) and c.get("name") and c.get("value") is not None:
                        cookies.append({
                            "name": str(c["name"]),
                            "value": str(c["value"]),
                            "domain": str(c.get("domain") or ".leclercdrive.fr"),
                            "path": str(c.get("path") or "/"),
                            "httpOnly": bool(c.get("httpOnly", False)),
                            "secure": bool(c.get("secure", True)),
                            "sameSite": str(c.get("sameSite", "Lax")).capitalize(),
                        })
            else:
                cookie_line = cleaned
                curl_match = re.search(r"(?:-b|--cookie|-H\s+\^?['\"]cookie\s*:)\s*([^\r\n]+)", cleaned, re.IGNORECASE)
                if curl_match:
                    cookie_line = curl_match.group(1).strip()
                    cookie_line = re.sub(r"[\^\\]*['\"]\s*[\^\\]*$", "", cookie_line).strip()
                    cookie_line = re.sub(r"\^([|&\"^<>%])", r"\1", cookie_line)
                elif cleaned.lower().startswith("cookie:"):
                    cookie_line = cleaned.split(":", 1)[1].strip()

                cookie_line = cookie_line.strip("'\"")
                if "=" not in cookie_line and len(cookie_line) >= 30:
                    cookies.append({"name": "datadome", "value": cookie_line, "domain": ".leclercdrive.fr", "path": "/", "secure": True, "sameSite": "Lax"})
                else:
                    for part in cookie_line.split(";"):
                        part = part.strip()
                        if "=" not in part:
                            continue
                        k, v = part.split("=", 1)
                        if k.strip() and v.strip():
                            cookies.append({"name": k.strip(), "value": v.strip(), "domain": ".leclercdrive.fr", "path": "/", "secure": True, "sameSite": "Lax"})

            if not cookies:
                return 0

            self.cookies_file.write_text(json.dumps(cookies, ensure_ascii=False, indent=2), encoding="utf-8")
            if self._context is not None:
                self._context.clear_cookies()
                self._context.add_cookies(cookies)
            self.log(f"🍪 {len(cookies)} cookie(s) de session enregistrés avec succès.")
            return len(cookies)
        except Exception as e:
            self.log(f"⚠️ Erreur lors de l'import des cookies : {e}")
            return 0

    def _cleanup_locks(self) -> None:
        for lock_name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
            lock_path = self.session_dir / lock_name
            try:
                if os.path.lexists(lock_path):
                    os.unlink(lock_path)
            except Exception:
                pass

    def close(self) -> None:
        if self._context is not None:
            try:
                self._context.close()
            except Exception:
                pass
            self._context = None
        self._page = None
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:
                pass
            self._playwright = None
        self._cleanup_locks()

    def _get_page(self):
        from playwright.sync_api import sync_playwright

        if self._context is not None and self._page is not None:
            try:
                if not self._page.is_closed():
                    return self._page
            except Exception:
                pass
            self.close()

        self._cleanup_locks()
        self._playwright = sync_playwright().start()

        chrome_exe = find_chrome_executable()
        launch_kwargs = {
            "user_data_dir": str(self.session_dir),
            "headless": False,
            "ignore_default_args": ["--enable-automation"],
            "args": list(CHROME_ARGS),
            "viewport": {"width": 1366, "height": 900},
            "locale": "fr-FR",
        }
        if chrome_exe:
            launch_kwargs["executable_path"] = chrome_exe

        self._context = self._playwright.chromium.launch_persistent_context(**launch_kwargs)
        self._context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")

        if self.cookies_file.exists():
            try:
                saved = json.loads(self.cookies_file.read_text(encoding="utf-8"))
                if isinstance(saved, list) and saved:
                    self._context.clear_cookies()
                    self._context.add_cookies(saved)
            except Exception:
                pass

        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        return self._page

    def save_page_screenshot(self, page=None) -> None:
        p = page or self._page
        if p and not p.is_closed():
            try:
                p.screenshot(path=str(self.screenshot_file))
            except Exception:
                pass

    def _dismiss_cookies(self, page) -> None:
        try:
            btn = page.locator("#didomi-notice-agree-button, .didomi-continue-without-agreeing")
            if btn.count() > 0 and btn.first.is_visible():
                btn.first.click(timeout=2000)
                page.wait_for_timeout(400)
            page.evaluate("""
                () => {
                    const host = document.getElementById('didomi-host');
                    if (host) host.remove();
                    document.body.classList.remove('didomi-popup-open');
                }
            """)
        except Exception:
            pass

    def get_cart_and_auth_state(self, page=None) -> dict:
        if page is None:
            page = self._get_page()
        try:
            return page.evaluate("""
                () => {
                    const userEl = document.querySelector('#spanWCSD334_Bonjour, #aWCSD334_Connecte');
                    const connecteDiv = document.querySelector('[id$="divWCSD334_Connecte"]');
                    const nonConnecteDiv = document.querySelector('[id$="divWCSD334_NonConnecte"]');
                    const dlConnected = (window.dataLayer && window.dataLayer[0] && window.dataLayer[0].Client_EstConnecte === 'oui');

                    let isConnected = Boolean(dlConnected);
                    if (connecteDiv && connecteDiv.classList.contains('afficher')) isConnected = true;
                    if (nonConnecteDiv && nonConnecteDiv.classList.contains('afficher')) isConnected = false;

                    const userName = userEl ? userEl.innerText.replace(/\\s+/g, ' ').trim() : '';
                    if (userName) isConnected = true;

                    let bestCount = 0;
                    const countNodes = document.querySelectorAll('.spanWCRS381_Notification, [class*="WCRS381"][class*="Notification"], #divWCRS381_Panier span');
                    for (const node of countNodes) {
                        const txt = (node.innerText || '').trim();
                        if (/^\\d+$/.test(txt)) {
                            const val = parseInt(txt, 10);
                            if (val > bestCount) bestCount = val;
                        }
                    }

                    let bestTotal = '0,00 €';
                    let bestTotalVal = 0.0;
                    const totalNodes = document.querySelectorAll('.strongWCRS381_Montant, [class*="WCRS381"][class*="Montant"], #divWCRS381_Panier strong');
                    for (const node of totalNodes) {
                        const txt = (node.innerText || '').replace(/\\s+/g, ' ').trim();
                        const m = txt.match(/(\\d+[.,]\\d{2})\\s*€/);
                        if (m) {
                            const val = parseFloat(m[1].replace(',', '.'));
                            if (val >= bestTotalVal) {
                                bestTotalVal = val;
                                bestTotal = `${m[1].replace('.', ',')} €`;
                            }
                        }
                    }

                    return {
                        connected: isConnected,
                        user_name: userName || (isConnected ? 'Client connecté' : ''),
                        cart_count: bestCount,
                        cart_total: bestTotal
                    };
                }
            """)
        except Exception:
            return {"connected": False, "user_name": "", "cart_count": 0, "cart_total": "0,00 €"}

    def _is_datadome_or_captcha(self, page) -> bool:
        try:
            head = page.content()[:4000].lower()
            return "captcha-delivery.com" in head or "datadome captcha" in head
        except Exception:
            return False

    def login_leclerc(self, email: str = "", password: str = "", session_token: str = "") -> dict:
        if session_token and session_token.strip():
            self.import_session_cookies(session_token)

        page = self._get_page()

        entry_slug = self.magasin_slug or "magasin-125401-125401-Vandoeuvre-les-Nancy"
        home_url = f"https://{self.fd_shard}-courses.leclercdrive.fr/{entry_slug}/accueil.aspx"
        self.log(f"🌐 Ouverture de {home_url}...")
        page.goto(home_url, wait_until="domcontentloaded")
        page.wait_for_timeout(2500)
        self.save_page_screenshot(page)

        if self._is_datadome_or_captcha(page):
            self.log("⚠️ Blocage anti-bot DataDome CAPTCHA détecté sur la page d'accueil.")
            self.close()
            return {"status": "captcha_blocked", "message": "Protection DataDome détectée. Importez vos cookies via l'interface."}

        self._dismiss_cookies(page)

        state = self.get_cart_and_auth_state(page)
        if state["connected"]:
            self.log(f"✅ Session E.Leclerc connectée ({state['user_name']}) | Panier : {state['cart_count']} art. ({state['cart_total']})")
            self.close()
            return {"status": "connected", "user_name": state["user_name"], "cart_total": state["cart_total"], "cart_count": state["cart_count"]}

        if not email or not password:
            self.close()
            return {"status": "missing_credentials", "message": "E-mail ou mot de passe non renseigné."}

        self.log("🔑 Clic sur 'Me connecter'...")
        login_btn = page.locator("a.aWCSD334_NonConnecte, a:has-text('Me connecter'):visible").first
        if login_btn.count() == 0:
            self.close()
            return {"status": "login_failed", "message": "Bouton 'Me connecter' introuvable."}

        login_btn.click(timeout=4000)
        page.wait_for_timeout(2000)
        self.save_page_screenshot(page)

        email_inp = page.locator("input.txtWCLD312_Login:visible, input[type='email']:visible").first
        if email_inp.count() == 0:
            self.close()
            return {"status": "login_failed", "message": "Champ e-mail introuvable."}

        email_inp.fill(email.strip())
        page.wait_for_timeout(350)

        cont_btn = page.locator("button.btnWCLD312_Continuer:visible, button:has-text('Continuer'):visible").first
        if cont_btn.count() > 0:
            cont_btn.click(timeout=4000)
        else:
            email_inp.press("Enter")

        for _ in range(8):
            page.wait_for_timeout(500)
            pwd_inp = page.locator("input.txtWCLD312_Mdp:visible, input[type='password']:visible").first
            if pwd_inp.count() > 0:
                break

        pwd_inp = page.locator("input.txtWCLD312_Mdp:visible, input[type='password']:visible").first
        if pwd_inp.count() == 0:
            self.save_page_screenshot(page)
            self.close()
            return {"status": "login_failed", "message": "Champ mot de passe non affiché (Captcha possible)."}

        pwd_inp.fill(password)
        page.wait_for_timeout(300)

        submit_btn = page.locator("button.btnWCLD312_Valider:visible, button:has-text('Me connecter'):visible").first
        if submit_btn.count() > 0:
            submit_btn.click(timeout=4000)
        else:
            pwd_inp.press("Enter")

        page.wait_for_timeout(3500)
        self.save_page_screenshot(page)

        otp_popin = page.locator("#divPopinOTP:visible, .otp-container:visible, input.otp-input:visible")
        if otp_popin.count() > 0:
            self.log("📩 Code de sécurité à 6 chiffres envoyé par e-mail par E.Leclerc.")
            return {"status": "otp_required", "message": "Code de sécurité à 6 chiffres envoyé par e-mail."}

        state = self.get_cart_and_auth_state(page)
        self.close()
        if state["connected"]:
            self.log(f"🎉 Connexion réussie pour {state['user_name']} !")
            return {"status": "connected", "user_name": state["user_name"], "cart_total": state["cart_total"], "cart_count": state["cart_count"]}

        return {"status": "login_failed", "message": "Échec de connexion automatique."}

    def submit_otp(self, code_str: str) -> dict:
        digits = re.sub(r"[^0-9]", "", code_str.strip())
        if len(digits) != 6:
            return {"status": "error", "message": "Le code doit comporter exactement 6 chiffres."}

        page = self._get_page()
        try:
            inputs = page.locator("input.otp-input:visible")
            if inputs.count() >= 6:
                for i in range(6):
                    inputs.nth(i).fill(digits[i])
                    page.wait_for_timeout(100)

            trust_cb = page.locator("#deviceDeConfiance")
            if trust_cb.count() > 0 and not trust_cb.first.is_checked():
                trust_cb.first.check(force=True)

            valider_btn = page.locator("#valider_WCLD003ValidationIdentiteOTP, button[type='submit']:visible").first
            if valider_btn.count() > 0:
                valider_btn.click(timeout=4000)
                page.wait_for_timeout(3500)

            state = self.get_cart_and_auth_state(page)
            if state["connected"]:
                self.log(f"✅ OTP validé avec succès pour {state['user_name']} !")
                return {"status": "connected", "user_name": state["user_name"], "cart_total": state["cart_total"], "cart_count": state["cart_count"]}
            return {"status": "error", "message": "Code OTP refusé ou expiré."}
        finally:
            self.close()

    def scrape_order_history(self, max_orders: int = 15) -> dict:
        self.log(f"📥 Aspiration automatique des {max_orders} dernières commandes...")
        page = self._get_page()
        try:
            slug = self.magasin_slug or "magasin-125401-125401-Vandoeuvre-les-Nancy"
            orders_url = f"https://{self.fd_shard}-espace-client.leclercdrive.fr/drive/{slug}/mes-commandes.aspx"
            page.goto(orders_url, wait_until="domcontentloaded")
            page.wait_for_timeout(2500)
            self._dismiss_cookies(page)

            detail_hrefs = page.evaluate("""
                () => {
                    const links = Array.from(document.querySelectorAll('a[href*="detail-commande.aspx"]'));
                    const seen = new Set();
                    const res = [];
                    for (const a of links) {
                        const h = a.getAttribute('href');
                        if (h && !seen.has(h)) {
                            seen.add(h);
                            res.push(new URL(h, window.location.href).href);
                        }
                    }
                    return res;
                }
            """)

            history = self.load_history()
            orders_by_id = {o["order_id"]: o for o in history.get("orders", [])}
            saved_count = 0

            for link in detail_hrefs[:max_orders]:
                try:
                    page.goto(link, wait_until="domcontentloaded")
                    page.wait_for_timeout(2000)
                    self._dismiss_cookies(page)
                    order_data = page.evaluate("""
                        () => {
                            const itemsNodes = document.querySelectorAll('li.liWCCD353_LigneArticle');
                            if (!itemsNodes.length) return null;
                            const caption = document.querySelector('#table-details-commandes');
                            const numEl = document.querySelector('#ctl00_ctl00_mainMutiUnivers_main_ascWCCD014_ResumeCommande_hlNumeroCommande');
                            const dateEl = document.querySelector('#ctl00_ctl00_mainMutiUnivers_main_ascWCCD014_ResumeCommande_lblCommandeInfo');
                            const totalEl = document.querySelector('.divWCCD014_DetailCommandeSelectionnee .tbEspaceClientMontant');

                            const headerText = [caption ? caption.innerText : '', numEl ? numEl.innerText : '', dateEl ? dateEl.innerText : ''].join(' ');
                            const items = [];
                            itemsNodes.forEach((li) => {
                                const st1 = (li.getAttribute('stitre1') || '').trim();
                                const st2 = (li.getAttribute('stitre2') || '').trim();
                                const idp = (li.getAttribute('iidproduit') || '').trim();
                                const h4 = li.querySelector('.divWCCD353_Titre h4');
                                const fullTitle = st1 ? (st2 ? `${st1} - ${st2}` : st1) : (h4 ? h4.innerText.replace(/\\s+/g, ' ').trim() : '');
                                const qtyEl = li.querySelector('.pWCCD353_Quantite');
                                const priceEl = li.querySelector('.pWCCD051_Prix');
                                let rayon = '';
                                const accordeon = li.closest('.divWCCD353_ContenuAccordeon');
                                if (accordeon && accordeon.previousElementSibling) {
                                    const h3 = accordeon.previousElementSibling.querySelector('h3');
                                    if (h3) rayon = h3.innerText.trim();
                                }
                                if (fullTitle) {
                                    items.push({
                                        leclerc_id: idp || null,
                                        name: fullTitle,
                                        category: rayon || null,
                                        qty_text: qtyEl ? qtyEl.innerText.trim() : 'x1',
                                        price_text: priceEl ? priceEl.innerText.trim() : '0,00 €'
                                    });
                                }
                            });
                            return { headerText, totalText: totalEl ? totalEl.innerText.trim() : '0,00 €', items };
                        }
                    """)
                    if order_data and order_data.get("items"):
                        hdr = order_data["headerText"]
                        om = re.search(r"N°?\s*(\d{5,})", hdr, re.IGNORECASE)
                        oid = om.group(1) if om else f"CMD-{int(time.time())}"
                        dm = re.search(r"(\d{2})/(\d{2})/(\d{4})", hdr)
                        iso_date = f"{dm.group(3)}-{dm.group(2)}-{dm.group(1)}" if dm else datetime.now().strftime("%Y-%m-%d")
                        tot_m = re.search(r"(\d+[.,]\d{2})", order_data["totalText"].replace("\xa0", " "))
                        tot = float(tot_m.group(1).replace(",", ".")) if tot_m else 0.0

                        parsed_items = []
                        for it in order_data["items"]:
                            qm = re.search(r"x\s*(\d+)", it["qty_text"])
                            qty = int(qm.group(1)) if qm else 1
                            pm = re.search(r"(\d+[.,]\d{2})", it["price_text"].replace("\xa0", " "))
                            line_tot = float(pm.group(1).replace(",", ".")) if pm else 0.0
                            parsed_items.append({
                                "leclerc_id": it["leclerc_id"],
                                "name": it["name"],
                                "category": it["category"],
                                "qty": qty,
                                "unit_price": round(line_tot / qty, 2) if qty > 0 else line_tot,
                                "line_total_eur": line_tot,
                            })

                        orders_by_id[oid] = {"order_id": oid, "date": iso_date, "total_eur": tot, "items": parsed_items}
                        saved_count += 1
                        self.log(f"  [+] Commande #{oid} ({iso_date} - {tot} €) enregistrée ({len(parsed_items)} articles)")
                except Exception as ex:
                    self.log(f"  ⚠️ Erreur sur une commande : {ex}")

            history["orders"] = sorted(orders_by_id.values(), key=lambda o: o["date"])
            self.save_history(history)
            self.log(f"💾 Historique mis à jour : {len(history['orders'])} commandes au total.")
            return {"status": "ok", "saved_orders": saved_count, "total_orders": len(history["orders"])}
        finally:
            self.close()

    def add_product_by_query(self, query: str, qty: int = 1, preferred_leclerc_id: str | None = None) -> dict:
        clean_q = query.strip()
        safe_qty = max(1, int(qty or 1))
        page = self._get_page()

        slug = self.magasin_slug or "magasin-125401-125401-Vandoeuvre-les-Nancy"
        url = f"https://{self.fd_shard}-courses.leclercdrive.fr/{slug}/recherche.aspx?TexteRecherche={quote_plus(clean_q)}"
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_timeout(2000)
        self._dismiss_cookies(page)

        q_tokens = significant_tokens(clean_q)
        cards = page.locator("li.liWCRS310_Product")
        n_cards = cards.count()

        if n_cards == 0:
            return {"status": "not_found", "reason": "Aucun produit trouvé sur le Drive"}

        chosen_idx = -1
        chosen_title = ""
        chosen_price = "0,00 €"

        for idx in range(min(n_cards, 10)):
            card = cards.nth(idx)
            info = card.evaluate("""
                (el) => ({
                    title: (el.querySelector('.divWCRS310_Libelle, [class*="Libelle"], h3, h4')?.innerText || '').replace(/\\s+/g, ' ').trim(),
                    price: (el.querySelector('.divWCRS310_Prix, [class*="Prix"]')?.innerText || '').replace(/\\s+/g, ' ').trim(),
                    id: el.getAttribute('iidproduit') || el.getAttribute('data-id') || ''
                })
            """)
            t = info.get("title", "")
            if preferred_leclerc_id and info.get("id") == preferred_leclerc_id:
                chosen_idx = idx
                chosen_title = t
                chosen_price = info.get("price", "0,00 €")
                break

            ok, hits = candidate_token_hits(t, q_tokens)
            if ok:
                chosen_idx = idx
                chosen_title = t
                chosen_price = info.get("price", "0,00 €")
                break

        if chosen_idx == -1:
            return {"status": "not_found", "reason": f"Aucun résultat ne correspond aux mots-clés de '{clean_q}' (non substitué)"}

        # Pause humaine anti-bridage Drive
        pause = random.uniform(3.2, 4.5)
        self.log(f"⏱️ Pause humaine anti-bridage ({pause:.1f}s) avant ajout de '{chosen_title}'...")
        page.wait_for_timeout(int(pause * 1000))

        target_card = cards.nth(chosen_idx)
        for click_i in range(safe_qty):
            btn = target_card.locator("a.aWCRS310_More, button.aWCRS310_More, a.aWCRS310_Plus, a.aWCRS310_Ajouter, .divWCRS310_Panier a").first
            if btn.count() == 0:
                break
            btn.click(timeout=3500)
            page.wait_for_timeout(1800)

        self.save_page_screenshot(page)
        state = self.get_cart_and_auth_state(page)
        return {
            "status": "added",
            "selected_product": chosen_title,
            "price": chosen_price,
            "qty": safe_qty,
            "cart_count_after": state["cart_count"],
            "cart_total_after": state["cart_total"],
        }
