"""
Moteur d'automatisation E.Leclerc Drive pour Home Assistant (Raspberry Pi / Linux).
Inclut :
1. Authentification automatique E.Leclerc Drive + support du code OTP à 6 chiffres par e-mail (avec case 'Appareil de confiance').
2. Aspiration 100 % automatique de l'historique des commandes (sans aucun clic manuel de l'utilisateur).
3. Récupération de la liste Google Keep (avec conversion automatique du Mot de passe d'application Google 16 caractères ou jeton OAuth en Master Token).
4. Moteur hybride Python déterministe + IA (entité conversation.* de Home Assistant OU qwen2.5:1.5b via Ollama par défaut avec keep_alive=0).
"""

from __future__ import annotations

import json
import logging
import os
import random
import re
import shutil
import subprocess
import time
import unicodedata
import urllib.request
from datetime import datetime
from pathlib import Path
from urllib.parse import quote_plus, urljoin

_LOGGER = logging.getLogger(__name__)

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
    "legume", "legumes",
    "fruit", "fruits",
    "viande", "viandes",
    "poisson", "poissons",
    "fromage", "fromages",
    "yaourt", "yaourts",
    "charcuterie", "charcuteries",
    "boisson", "boissons",
    "gouter", "gouters",
    "apero", "aperitif",
}

PLURAL_GENERIC_WORDS = {
    "legumes", "fruits", "viandes", "poissons", "fromages",
    "yaourts", "charcuteries", "boissons", "gouters",
}

GENERIC_CATEGORY_HINTS = {
    "legume": "Fruits Légumes",
    "fruit": "Fruits Légumes",
    "viande": "Viandes Poissons",
    "poisson": "Viandes Poissons",
    "fromage": "Laitier Oeufs Végétal",
    "lait": "Laitier Oeufs Végétal",
    "yaourt": "Laitier Oeufs Végétal",
    "apero": "Epicerie salée",
    "chips": "Epicerie salée",
    "gouter": "Epicerie sucrée",
    "boisson": "Boissons",
    "surgele": "Surgelés",
}


def _norm_text(text: str) -> str:
    replaced = text.lower().replace("œ", "oe").replace("æ", "ae")
    nfkd = unicodedata.normalize("NFKD", replaced)
    no_accents = "".join(c for c in nfkd if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", no_accents).strip()


def _significant_tokens(text: str, exclude_generic: bool = False) -> list[str]:
    stop = {
        "de", "du", "des", "la", "le", "les", "au", "aux", "en", "et", "ou",
        "un", "une", "pour", "avec", "sans", "frais", "fraiche", "saison", "bio",
    }
    tokens = []
    for tok in _norm_text(text).split():
        if len(tok) <= 2 or tok in stop:
            continue
        if exclude_generic and tok in GENERIC_WORDS:
            continue
        stem = tok[:-1] if (len(tok) > 4 and tok.endswith(("s", "x"))) else tok
        tokens.append(stem)
    return tokens


def _candidate_token_hits(cand_title: str, query_tokens: list[str]) -> tuple[bool, int]:
    """Vérifie strictement que le produit correspond bien aux mots-clés recherchés."""
    if not query_tokens:
        return False, 0
    cand_words = [
        (w[:-1] if (len(w) > 4 and w.endswith(("s", "x"))) else w)
        for w in _norm_text(cand_title).split()
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


def _product_subtype(norm_name: str) -> str:
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


def find_chrome_executable() -> str | None:
    for candidate in CHROME_CANDIDATES:
        if Path(candidate).exists():
            return candidate
        which_path = shutil.which(Path(candidate).name)
        if which_path:
            return which_path
    return None


def _ensure_playwright_runtime() -> str | None:
    """
    Sur Home Assistant (Alpine Linux / Python 3.14), la roue musllinux de Playwright
    n'embarque pas Node.js ni Chromium par défaut.
    Cette fonction :
    1. Vérifie la présence de 'node' et 'chromium' (et les installe automatiquement via apk/apt si absents).
    2. Définit automatiquement PLAYWRIGHT_NODEJS_PATH=/usr/bin/node avant de lancer Playwright.
    """
    node_bin = os.environ.get("PLAYWRIGHT_NODEJS_PATH") or shutil.which("node") or shutil.which("nodejs")
    chrome_bin = find_chrome_executable()

    if (not node_bin or not Path(node_bin).exists()) or not chrome_bin:
        if Path("/sbin/apk").exists():
            _LOGGER.info("Installation automatique de nodejs et chromium dans le conteneur Alpine Home Assistant...")
            subprocess.run(
                [
                    "/sbin/apk", "add", "--no-cache",
                    "nodejs", "chromium", "nss", "freetype",
                    "harfbuzz", "ca-certificates", "ttf-freefont",
                ],
                check=False,
            )
        elif Path("/usr/bin/apt-get").exists():
            _LOGGER.info("Installation automatique de nodejs et chromium via apt-get...")
            subprocess.run(["/usr/bin/apt-get", "update"], check=False)
            subprocess.run(["/usr/bin/apt-get", "install", "-y", "nodejs", "chromium"], check=False)

        node_bin = shutil.which("node") or shutil.which("nodejs") or "/usr/bin/node"
        chrome_bin = find_chrome_executable()

    if node_bin and Path(node_bin).exists():
        os.environ["PLAYWRIGHT_NODEJS_PATH"] = str(node_bin)

    return chrome_bin


def parse_store_input(raw_input: str) -> tuple[str, str | None, str | None]:
    """
    Analyse ce que l'utilisateur a saisi pour son magasin :
    - Soit une URL complète copiée depuis son navigateur :
      ex: 'https://fd8-courses.leclercdrive.fr/magasin-012301-012301-Bourg-en-Bresse/accueil.aspx'
      -> retourne (shard='fd8', slug='magasin-012301-012301-Bourg-en-Bresse', search_query=None)
    - Soit un slug direct :
      ex: 'magasin-125401-125401-Vandoeuvre-les-Nancy'
      -> retourne (shard='fd2', slug='magasin-125401-125401-Vandoeuvre-les-Nancy', search_query=None)
    - Soit une Ville ou un Code Postal (ou vide) :
      ex: '54500', 'Bordeaux', ''
      -> retourne (shard='fd2', slug=None, search_query='54500') pour détection automatique !
    """
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


def resolve_google_keep_master_token(google_email: str, secret_or_token: str) -> str:
    """
    Convertit automatiquement un Mot de passe d'application Google (16 caractères)
    ou un jeton oauth2_4/... en Master Token Google Keep permanent (aas_et/...).
    """
    cleaned = secret_or_token.strip()
    if cleaned.startswith("aas_et/"):
        return cleaned

    import gpsoauth

    android_id = "0123456789abcdef"
    # Cas 1 : Jeton OAuth EmbeddedSetup (oauth2_4/...)
    if cleaned.startswith("oauth2_4/"):
        res = gpsoauth.exchange_token(google_email.strip(), cleaned, android_id)
        if "Token" in res:
            return res["Token"]
        raise ValueError(f"Échec de l'échange du token OAuth Google Keep : {res}")

    # Cas 2 : Mot de passe d'application Google (16 lettres type 'xxxx xxxx xxxx xxxx')
    app_pwd = cleaned.replace(" ", "")
    res = gpsoauth.perform_master_login(google_email.strip(), app_pwd, android_id)
    if "Token" in res:
        return res["Token"]

    raise ValueError(
        f"Google a refusé la connexion directe par mot de passe d'application ({res.get('Error', res)}). "
        "Utilisez un Master Token 'aas_et/...' ou un token 'oauth2_4/...'."
    )


def fetch_google_keep_items(google_email: str, master_token: str, note_title: str = "Liste de courses") -> list[str]:
    """Récupère les articles non cochés de la note Google Keep."""
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


class LeclercDriveWorker:
    """Gère la session Playwright, l'historique et le panier E.Leclerc Drive."""

    def __init__(self, storage_dir: Path, magasin_slug: str = "", plural_count: int = 3) -> None:
        self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.session_dir = self.storage_dir / "leclerc_session"
        self.history_file = self.storage_dir / "history.json"

        shard, parsed_slug, search_hint = parse_store_input(magasin_slug)
        self.fd_shard = shard
        self.magasin_slug = parsed_slug or ""
        self.store_search_hint = search_hint
        self.plural_count = max(1, int(plural_count or 3))

        self._playwright = None
        self._context = None
        self._page = None

    def _update_store_from_url(self, current_url: str) -> None:
        """Extrait automatiquement le serveur régional (fd1..fd16) et le slug du magasin depuis l'URL courante."""
        shard, slug, _ = parse_store_input(current_url)
        if slug:
            self.fd_shard = shard
            self.magasin_slug = slug

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
        # Configure automatiquement PLAYWRIGHT_NODEJS_PATH et installe nodejs/chromium si nécessaire sur Alpine HA
        chrome_exe = _ensure_playwright_runtime()
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
        self._context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        return self._page

    def _dismiss_cookies(self, page) -> None:
        try:
            btn = page.locator("#didomi-notice-agree-button")
            if btn.count() > 0 and btn.first.is_visible():
                btn.first.click(timeout=2000)
                page.wait_for_timeout(400)
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

    def _detect_store_on_page(self, page) -> None:
        """Extrait le serveur régional (fdX) et le slug (magasin-...) depuis l'URL courante ou les liens de la page."""
        self._update_store_from_url(page.url)
        if self.magasin_slug:
            return
        try:
            detected_href = page.evaluate("""
                () => {
                    const links = Array.from(document.querySelectorAll('a[href*="magasin-"]'));
                    for (const a of links) {
                        const h = a.href || a.getAttribute('href') || '';
                        if (/magasin-\\d+/i.test(h)) return h;
                    }
                    return '';
                }
            """)
            if detected_href:
                self._update_store_from_url(detected_href)
        except Exception:
            pass

    def login_leclerc(self, email: str, password: str) -> dict:
        """
        Connecte automatiquement l'utilisateur à son compte E.Leclerc Drive depuis Home Assistant.
        Détecte automatiquement son magasin rattaché (ou recherche sa Ville / Code Postal) s'il n'a pas fourni le slug.
        """
        page = self._get_page()
        if self.magasin_slug:
            home_url = f"https://{self.fd_shard}-courses.leclercdrive.fr/{self.magasin_slug}/accueil.aspx"
        else:
            home_url = "https://www.leclercdrive.fr/"

        page.goto(home_url, wait_until="domcontentloaded")
        page.wait_for_timeout(2000)
        self._dismiss_cookies(page)
        self._detect_store_on_page(page)

        # Si l'utilisateur a saisi un Code Postal ou une Ville (ex: '54500' ou 'Lyon') sur www.leclercdrive.fr
        if not self.magasin_slug and self.store_search_hint:
            try:
                search_inp = page.locator(
                    "input[placeholder*='code postal' i]:visible, input[placeholder*='ville' i]:visible, "
                    "input[id*='Recherche' i]:visible, input[class*='Recherche' i]:visible"
                ).first
                if search_inp.count() > 0:
                    search_inp.fill(self.store_search_hint)
                    page.wait_for_timeout(1200)
                    search_inp.press("Enter")
                    page.wait_for_timeout(2500)
                    choose_drive = page.locator("a[href*='magasin-']:visible").first
                    if choose_drive.count() > 0:
                        choose_drive.click(timeout=4000)
                        page.wait_for_timeout(2500)
                        self._detect_store_on_page(page)
            except Exception:
                pass

        state = self.get_cart_and_auth_state(page)
        if state["connected"]:
            self._detect_store_on_page(page)
            self.close()
            return {
                "status": "connected",
                "user_name": state["user_name"],
                "cart_total": state["cart_total"],
                "detected_store": f"https://{self.fd_shard}-courses.leclercdrive.fr/{self.magasin_slug}" if self.magasin_slug else "",
            }

        # Clique sur 'Me connecter'
        login_btn = page.locator("a.aWCSD334_NonConnecte, a:has-text('Me connecter'):visible").first
        if login_btn.count() > 0 and login_btn.is_visible():
            login_btn.click(timeout=4000)
            page.wait_for_timeout(2500)

        state = self.get_cart_and_auth_state(page)
        if state["connected"]:
            self._detect_store_on_page(page)
            self.close()
            return {
                "status": "connected",
                "user_name": state["user_name"],
                "cart_total": state["cart_total"],
                "detected_store": f"https://{self.fd_shard}-courses.leclercdrive.fr/{self.magasin_slug}" if self.magasin_slug else "",
            }

        # Remplit le champ email/login
        email_input = page.locator(
            "input[type='email']:visible, input[name*='Email' i]:visible, "
            "input[name*='Login' i]:visible, input[id*='Email' i]:visible, input[id*='Login' i]:visible"
        ).first
        if email_input.count() > 0:
            email_input.fill(email.strip())
            page.wait_for_timeout(400)

        pwd_input = page.locator("input[type='password']:visible").first
        if pwd_input.count() == 0:
            next_btn = page.locator("button[type='submit']:visible, input[type='submit']:visible, a.bouton-bleu:visible").first
            if next_btn.count() > 0:
                next_btn.click(timeout=3000)
                page.wait_for_timeout(2000)
            pwd_input = page.locator("input[type='password']:visible").first

        if pwd_input.count() > 0:
            pwd_input.fill(password)
            page.wait_for_timeout(400)
            submit_btn = page.locator(
                "button[type='submit']:visible, input[type='submit']:visible, "
                "a[id*='Valider' i]:visible, a[class*='Valider' i]:visible, button.bouton-bleu:visible"
            ).first
            if submit_btn.count() > 0:
                submit_btn.click(timeout=4000)
            else:
                pwd_input.press("Enter")
            page.wait_for_timeout(3500)

        self._detect_store_on_page(page)

        # Vérifie si la popin de code à 6 chiffres par e-mail (#divPopinOTP) est affichée
        otp_popin = page.locator("#divPopinOTP:visible, .otp-container:visible")
        if otp_popin.count() > 0:
            return {
                "status": "otp_required",
                "message": "Un code de sécurité à 6 chiffres a été envoyé par e-mail par E.Leclerc.",
            }

        state = self.get_cart_and_auth_state(page)
        self._detect_store_on_page(page)
        self.close()
        if state["connected"]:
            return {
                "status": "connected",
                "user_name": state["user_name"],
                "cart_total": state["cart_total"],
                "detected_store": f"https://{self.fd_shard}-courses.leclercdrive.fr/{self.magasin_slug}" if self.magasin_slug else "",
            }

        return {"status": "login_failed", "message": "Impossible de valider la connexion automatiquement (vérifiez vos identifiants ou Captcha)."}

    def submit_leclerc_otp(self, otp_code: str) -> dict:
        """
        Saisit le code à 6 chiffres reçu par e-mail dans la popin #divPopinOTP,
        coche automatiquement la case 'Appareil de confiance' (#deviceDeConfiance)
        et valide la connexion.
        """
        digits = re.sub(r"[^0-9]", "", otp_code.strip())
        if len(digits) != 6:
            return {"status": "invalid_otp", "message": "Le code doit contenir exactement 6 chiffres."}

        page = self._get_page()
        try:
            otp_inputs = page.locator("input.otp-input:visible")
            if otp_inputs.count() >= 6:
                for i in range(6):
                    otp_inputs.nth(i).fill(digits[i])
                    page.wait_for_timeout(100)

            trust_cb = page.locator("#deviceDeConfiance")
            if trust_cb.count() > 0 and not trust_cb.first.is_checked():
                trust_cb.first.check(force=True)

            valider_btn = page.locator("#valider_WCLD003ValidationIdentiteOTP, button[type='submit']:visible").first
            if valider_btn.count() > 0:
                valider_btn.click(timeout=4000)
                page.wait_for_timeout(3500)

            self._detect_store_on_page(page)
            state = self.get_cart_and_auth_state(page)
            if state["connected"]:
                return {
                    "status": "connected",
                    "user_name": state["user_name"],
                    "cart_total": state["cart_total"],
                    "detected_store": f"https://{self.fd_shard}-courses.leclercdrive.fr/{self.magasin_slug}" if self.magasin_slug else "",
                }
            return {"status": "otp_failed", "message": "Code refusé ou expiré."}
        finally:
            self.close()

    def auto_scrape_order_history(self, max_orders: int = 15) -> dict:
        """
        Parcourt AUTOMATIQUEMENT la page 'Mes commandes' d'E.Leclerc Drive et extrait
        les 'max_orders' dernières commandes dans history.json sans aucun clic manuel !
        """
        page = self._get_page()
        try:
            slug_part = self.magasin_slug or "magasin-125401-125401-Vandoeuvre-les-Nancy"
            orders_url = f"https://{self.fd_shard}-espace-client.leclercdrive.fr/drive/{slug_part}/mes-commandes.aspx"
            page.goto(orders_url, wait_until="domcontentloaded")
            page.wait_for_timeout(2500)
            self._dismiss_cookies(page)

            # Récupère tous les liens vers 'detail-commande.aspx' présents dans la liste des commandes
            detail_hrefs = page.evaluate("""
                () => {
                    const links = Array.from(document.querySelectorAll('a[href*="detail-commande.aspx"]'));
                    const unique = [];
                    const seen = new Set();
                    for (const a of links) {
                        const href = a.getAttribute('href');
                        if (href && !seen.has(href)) {
                            seen.add(href);
                            unique.push(new URL(href, window.location.href).href);
                        }
                    }
                    return unique;
                }
            """)

            history = self.load_history()
            orders_by_id = {o["order_id"]: o for o in history.get("orders", [])}
            newly_imported = 0

            for detail_url in detail_hrefs[:max_orders]:
                page.goto(detail_url, wait_until="domcontentloaded")
                page.wait_for_timeout(1800)
                order = self._extract_order_from_detail_page(page)
                if order and order.get("items"):
                    orders_by_id[order["order_id"]] = order
                    newly_imported += 1

            history["last_updated"] = datetime.now().strftime("%Y-%m-%d")
            history["orders"] = sorted(orders_by_id.values(), key=lambda o: o["date"])
            self.history_file.write_text(json.dumps(history, indent=2, ensure_ascii=False), encoding="utf-8")

            return {
                "status": "ok",
                "newly_imported": newly_imported,
                "total_orders": len(history["orders"]),
            }
        finally:
            self.close()

    def _extract_order_from_detail_page(self, page) -> dict | None:
        try:
            data = page.evaluate("""
                () => {
                    const itemsNodes = document.querySelectorAll('li.liWCCD353_LigneArticle');
                    if (!itemsNodes.length) return null;
                    const caption = document.querySelector('#table-details-commandes');
                    const numEl = document.querySelector('#ctl00_ctl00_mainMutiUnivers_main_ascWCCD014_ResumeCommande_hlNumeroCommande');
                    const dateEl = document.querySelector('#ctl00_ctl00_mainMutiUnivers_main_ascWCCD014_ResumeCommande_lblCommandeInfo');
                    const totalEl = document.querySelector('.divWCCD014_DetailCommandeSelectionnee .tbEspaceClientMontant');

                    const headerText = [
                        caption ? caption.innerText : '',
                        numEl ? numEl.innerText : '',
                        dateEl ? dateEl.innerText : ''
                    ].join(' ');

                    const items = [];
                    itemsNodes.forEach((li) => {
                        const stitre1 = (li.getAttribute('stitre1') || '').trim();
                        const stitre2 = (li.getAttribute('stitre2') || '').trim();
                        const idProduit = (li.getAttribute('iidproduit') || '').trim();
                        const h4 = li.querySelector('.divWCCD353_Titre h4');
                        const fullTitle = stitre1
                            ? (stitre2 ? `${stitre1} - ${stitre2}` : stitre1)
                            : (h4 ? h4.innerText.replace(/\\s+/g, ' ').trim() : '');
                        const qtyEl = li.querySelector('.pWCCD353_Quantite');
                        const priceEl = li.querySelector('.pWCCD051_Prix');

                        let rayon = '';
                        const accordeonContent = li.closest('.divWCCD353_ContenuAccordeon');
                        if (accordeonContent && accordeonContent.previousElementSibling) {
                            const h3 = accordeonContent.previousElementSibling.querySelector('h3');
                            if (h3) rayon = h3.innerText.trim();
                        }

                        if (fullTitle) {
                            items.push({
                                leclerc_id: idProduit || null,
                                name: fullTitle,
                                category: rayon || null,
                                qty_text: qtyEl ? qtyEl.innerText.trim() : 'x1',
                                price_text: priceEl ? priceEl.innerText.trim() : '0,00 €'
                            });
                        }
                    });

                    return {
                        header_text: headerText,
                        total_text: totalEl ? totalEl.innerText.trim() : '0,00 €',
                        items
                    };
                }
            """)
        except Exception:
            return None

        if not data or not data.get("items"):
            return None

        header_text = data["header_text"]
        order_match = re.search(r"N°?\s*(\d{5,})", header_text, re.IGNORECASE)
        order_id = order_match.group(1) if order_match else f"CMD-{datetime.now().strftime('%Y%m%d%H%M%S')}"

        match_fr = re.search(r"(\d{2})/(\d{2})/(\d{4})", header_text)
        iso_date = f"{match_fr.group(3)}-{match_fr.group(2)}-{match_fr.group(1)}" if match_fr else datetime.now().strftime("%Y-%m-%d")

        def _parse_p(txt: str) -> float:
            m = re.search(r"(\d+[.,]\d{2})", txt.replace("\xa0", " "))
            return float(m.group(1).replace(",", ".")) if m else 0.0

        total_eur = _parse_p(data["total_text"])
        parsed_items = []
        for it in data["items"]:
            m_q = re.search(r"x\s*(\d+)", it["qty_text"], re.IGNORECASE)
            qty = int(m_q.group(1)) if m_q else 1
            line_total = _parse_p(it["price_text"])
            unit_price = round(line_total / qty, 2) if qty > 0 else line_total
            parsed_items.append({
                "leclerc_id": it["leclerc_id"],
                "name": it["name"],
                "category": it["category"],
                "qty": qty,
                "unit_price": unit_price,
                "line_total_eur": line_total,
            })

        return {
            "order_id": order_id,
            "date": iso_date,
            "total_eur": total_eur,
            "items": parsed_items,
        }

    def load_history(self) -> dict:
        if not self.history_file.exists():
            return {"last_updated": None, "orders": []}
        try:
            return json.loads(self.history_file.read_text(encoding="utf-8"))
        except Exception:
            return {"last_updated": None, "orders": []}

    def compute_stats(self, max_products: int = 300) -> dict:
        data = self.load_history()
        orders = data.get("orders", [])
        if not orders:
            return {"orders_count": 0, "avg_basket_eur": 0.0, "products": []}

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
        avg_days_between_orders = round(sum(order_intervals) / len(order_intervals), 1) if order_intervals else 14.0
        last_order_date_str = sorted_orders[-1].get("date") if sorted_orders else None
        days_since_last_order = (today - order_dates[-1]).days if order_dates else 14

        if days_since_last_order <= 5:
            shopping_mode = "appoint_recent"
            shopping_mode_label = (
                f"Courses rapprochées (dernière commande il y a {days_since_last_order}j le {last_order_date_str} "
                "-> éviter de racheter les stocks de la dernière commande, quantités modérées)"
            )
        elif days_since_last_order >= max(18, int(avg_days_between_orders * 1.25)):
            shopping_mode = "giga_courses_frigo_a_sec"
            shopping_mode_label = (
                f"Giga courses / Frigo à sec (dernière commande il y a {days_since_last_order}j le {last_order_date_str}, "
                f"moyenne habituelle {avg_days_between_orders}j -> rechargement complet frais + surgelés)"
            )
        else:
            shopping_mode = "courses_normales"
            shopping_mode_label = (
                f"Courses hebdomadaires normales (dernière commande il y a {days_since_last_order}j le {last_order_date_str}, "
                f"moyenne habituelle {avg_days_between_orders}j -> rotation par rapport à la dernière commande)"
            )

        last_order_items = sorted_orders[-1].get("items", []) if sorted_orders else []
        last_order_keys = {str(it.get("leclerc_id") or it.get("name", "")).strip() for it in last_order_items}

        product_history: dict[str, dict] = {}

        for order in sorted_orders:
            order_date_str = order.get("date")
            try:
                order_date = datetime.strptime(order_date_str, "%Y-%m-%d").date() if order_date_str else None
            except ValueError:
                order_date = None

            seen_in_order: set[str] = set()
            for item in order.get("items", []):
                key = str(item.get("leclerc_id") or item.get("name", "")).strip()
                if not key:
                    continue
                entry = product_history.setdefault(
                    key,
                    {
                        "leclerc_id": item.get("leclerc_id"),
                        "name": item.get("name", ""),
                        "category": item.get("category"),
                        "occurrences": 0,
                        "quantities": [],
                        "unit_prices": [],
                        "dates": [],
                    },
                )
                qty = int(item.get("qty", 1) or 1)
                unit_price = float(item.get("unit_price", item.get("line_total_eur", 0.0)) or 0.0)
                entry["quantities"].append(qty)
                if unit_price > 0:
                    entry["unit_prices"].append(unit_price)
                if key not in seen_in_order:
                    entry["occurrences"] += 1
                    seen_in_order.add(key)
                    if order_date:
                        entry["dates"].append(order_date)

        summary_products = []
        for key, entry in product_history.items():
            dates = sorted(entry["dates"])
            intervals = [(dates[i] - dates[i - 1]).days for i in range(1, len(dates)) if (dates[i] - dates[i - 1]).days > 0]
            avg_interval = round(sum(intervals) / len(intervals), 1) if intervals else None
            days_since_last = (today - dates[-1]).days if dates else None
            suggested_qty = max(1, round(sum(entry["quantities"]) / len(entry["quantities"]))) if entry["quantities"] else 1
            avg_price = round(sum(entry["unit_prices"]) / len(entry["unit_prices"]), 2) if entry["unit_prices"] else 0.0
            due_for_restock = bool(
                entry["occurrences"] >= 3
                and avg_interval is not None
                and days_since_last is not None
                and days_since_last >= max(7, avg_interval * 0.85)
            )

            summary_products.append({
                "leclerc_id": entry["leclerc_id"],
                "name": entry["name"],
                "category": entry["category"],
                "occurrences": entry["occurrences"],
                "avg_interval_days": avg_interval,
                "days_since_last": days_since_last,
                "in_last_order": key in last_order_keys,
                "due_for_restock": due_for_restock,
                "suggested_qty": suggested_qty,
                "unit_price": avg_price,
            })

        summary_products.sort(key=lambda p: (-(p["occurrences"]), -(p["days_since_last"] or 0)))
        return {
            "orders_count": len(sorted_orders),
            "avg_basket_eur": avg_basket,
            "avg_days_between_orders": avg_days_between_orders,
            "last_order_date": last_order_date_str,
            "days_since_last_order": days_since_last_order,
            "shopping_mode": shopping_mode,
            "shopping_mode_label": shopping_mode_label,
            "last_order_items": [it.get("name", "") for it in last_order_items if it.get("name")],
            "products": summary_products[:max_products],
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
            seasonal_veg = ("champignon", "poireau", "chou", "brocoli", "endive", "carotte", "butternut", "courge", "epinard", "salade", "laitue", "mache", "pomme de terre")
            out_of_season_veg = ("tomate", "courgette", "concombre", "aubergine", "poivron")
            seasonal_fruits = ("pomme", "poire", "kiwi", "mandarine", "clementine", "orange", "citron", "banane")
            out_of_season_fruits = ("abricot", "nectarine", "peche", "fraise", "cerise", "melon", "myrtille", "prune", "raisin")
        elif m in (3, 4, 5):
            season = "Printemps"
            seasonal_veg = ("asperge", "radis", "epinard", "salade", "laitue", "carotte", "champignon", "chou", "poireau", "brocoli", "pomme de terre")
            out_of_season_veg = ("butternut", "courge", "tomate", "aubergine", "poivron")
            seasonal_fruits = ("fraise", "pomme", "poire", "kiwi", "banane", "citron")
            out_of_season_fruits = ("abricot", "nectarine", "peche", "melon", "raisin", "prune", "mandarine")
        elif m in (6, 7, 8):
            season = "Été"
            seasonal_veg = ("tomate", "courgette", "concombre", "aubergine", "poivron", "haricot", "salade", "laitue", "roquette", "feuille de chene")
            out_of_season_veg = ("endive", "butternut", "courge", "chou chinois", "poireau")
            seasonal_fruits = ("abricot", "nectarine", "peche", "melon", "fraise", "cerise", "myrtille", "prune", "framboise")
            out_of_season_fruits = ("mandarine", "clementine", "kiwi")
        else:
            season = "Automne"
            seasonal_veg = ("champignon", "brocoli", "chou", "poireau", "carotte", "butternut", "courge", "epinard", "salade", "laitue", "feuille de chene", "endive", "pomme de terre")
            out_of_season_veg = ("tomate", "concombre", "courgette", "aubergine", "poivron")
            seasonal_fruits = ("pomme", "poire", "kiwi", "raisin", "mandarine", "clementine", "prune", "banane", "citron")
            out_of_season_fruits = ("abricot", "nectarine", "peche", "fraise", "cerise", "melon")

        return {
            "date_str": now.strftime("%Y-%m-%d"),
            "month_name": month_name,
            "season": season,
            "seasonal_veg": seasonal_veg,
            "out_of_season_veg": out_of_season_veg,
            "seasonal_fruits": seasonal_fruits,
            "out_of_season_fruits": out_of_season_fruits,
            "summary": f"{season} ({month_name} en France)",
        }

    def find_history_match(self, keep_item: str, products: list[dict]) -> dict | None:
        tokens = _significant_tokens(keep_item, exclude_generic=True)
        if not tokens:
            return None

        best_prod = None
        best_score = 0
        for prod in products:
            prod_words = set(_norm_text(prod.get("name", "")).split())
            hits = sum(
                1
                for t in tokens
                if any(w.startswith(t) or t.startswith(w) for w in prod_words if len(w) >= 3)
            )
            if hits == 0 or (len(tokens) <= 2 and hits < len(tokens)):
                continue
            score = hits * 100 + int(prod.get("occurrences", 0)) * 5
            if score > best_score:
                best_score = score
                best_prod = prod
        return best_prod

    def find_semantic_family_products(
        self,
        generic_item: str,
        products: list[dict],
        stats: dict | None = None,
        arbitrations_out: list[str] | None = None,
    ) -> list[dict]:
        stats = stats or {}
        shopping_mode = stats.get("shopping_mode", "courses_normales")
        last_order_date = stats.get("last_order_date", "récente")
        season_ctx = self.get_french_season_context()

        m = re.search(r"\b(\d+)\b", generic_item)
        if m:
            count = max(1, min(6, int(m.group(1))))
        elif bool(set(_norm_text(generic_item).split()) & PLURAL_GENERIC_WORDS):
            if shopping_mode == "appoint_recent":
                count = 2
            elif shopping_mode == "giga_courses_frigo_a_sec":
                count = max(3, self.plural_count + 1)
            else:
                count = self.plural_count
        else:
            count = 1

        norm_item = _norm_text(generic_item)
        matching_pool: list[dict] = []
        skipped_seasonal: list[str] = []
        skipped_last_order: list[str] = []

        for p in products:
            pname = _norm_text(p.get("name", ""))
            pcat = _norm_text(p.get("category") or "")

            if "viande" in norm_item:
                is_meat = any(
                    w in pname
                    for w in (
                        "steak", "viande", "boeuf", "bovine", "cordon bleu", "cordons bleus",
                        "filet de poulet", "filets de poulet", "aiguillette", "blanc de poulet",
                        "boulette", "chipolata", "merguez", "kebab", "nugget", "emince de porc", "dinde",
                    )
                )
                is_false = any(bad in pname for bad in ("chips", "saveur", "gout", "pizza", "gnocchi", "tortellini", "ravioli", "sauce", "grisons"))
                if is_meat and not is_false:
                    matching_pool.append(p)
            elif "legume" in norm_item:
                is_fruit_or_drink = any(
                    bad in pname
                    for bad in (
                        "jus", "brique", "nectar", "sirop", "abricot", "nectarine", "peche",
                        "poire", "kiwi", "banane", "raisin", "fraise", "cerise", "melon",
                        "myrtille", "prune", "mandarine", "citron", "mangue", "pink lady",
                        "pommes bicolores", "graines de", "pignons de", "cerises", "fraises",
                    )
                )
                if "frais" in norm_item or "saison" in norm_item:
                    is_veg = "fruits legumes" in pcat and not is_fruit_or_drink
                else:
                    is_veg = ("fruits legumes" in pcat or "surgele" in pcat) and (
                        "fruits legumes" in pcat
                        or any(v in pname for v in ("brocoli", "courgette", "champignon", "haricot", "epinard", "chou", "poireau"))
                    )
                if is_veg and not is_fruit_or_drink and "fromage blanc" not in pname:
                    if any(oos in pname for oos in season_ctx["out_of_season_veg"]):
                        skipped_seasonal.append(p["name"].split(" - ")[0])
                        continue
                    matching_pool.append(p)
            elif "fruit" in norm_item:
                if "fruits legumes" in pcat and any(
                    f in pname for f in ("pomme", "poire", "banane", "kiwi", "abricot", "nectarine", "peche", "raisin", "fraise", "cerise", "melon", "myrtille", "prune", "mandarine", "mangue", "citron")
                ) and "pomme de terre" not in pname and "pommes de terre" not in pname and "jus" not in pname:
                    if any(oos in pname for oos in season_ctx["out_of_season_fruits"]):
                        skipped_seasonal.append(p["name"].split(" - ")[0])
                        continue
                    matching_pool.append(p)
            elif "fromage" in norm_item:
                if any(w in pname for w in ("fromage", "chaource", "parmigiano", "parmesan", "boursin", "babybel", "emmental", "comte", "chevre", "mozzarella", "camembert")) and not any(
                    bad in pname for bad in ("chips", "gnocchi", "tortellini", "ravioli", "pizza", "concombre")
                ):
                    matching_pool.append(p)
            elif "yaourt" in norm_item:
                if any(w in pname for w in ("yaourt", "veloute", "yoplait", "danone", "activia", "petit suisse")):
                    matching_pool.append(p)
            elif "charcuterie" in norm_item:
                if any(w in pname for w in ("jambon", "lardon", "saucisson", "knack", "chorizo", "bacon")) and "gnocchi" not in pname:
                    matching_pool.append(p)

        if skipped_seasonal and arbitrations_out is not None:
            uniq_s = list(dict.fromkeys(skipped_seasonal))[:4]
            arbitrations_out.append(
                f"🍂 Saisonnalité ({season_ctx['summary']}) pour '{generic_item}' : hors-saison écartés ({', '.join(uniq_s)})."
            )

        subtype_totals: dict[str, int] = {}
        for p in matching_pool:
            st = _product_subtype(_norm_text(p.get("name", "")))
            subtype_totals[st] = subtype_totals.get(st, 0) + int(p.get("occurrences", 0))

        def smart_sort_key(prod: dict) -> tuple[int, int, int, int]:
            in_last = bool(prod.get("in_last_order"))
            pname = _norm_text(prod.get("name", ""))
            st = _product_subtype(pname)
            in_season = 1 if any(s in pname for s in (season_ctx["seasonal_veg"] + season_ctx["seasonal_fruits"])) else 0
            return (0 if in_last else 1, in_season, subtype_totals.get(st, 0), int(prod.get("occurrences", 0)))

        matching_pool.sort(key=smart_sort_key, reverse=True)

        max_freezer_packs = 2 if shopping_mode == "giga_courses_frigo_a_sec" else 1
        freezer_taken = 0
        selected: list[dict] = []
        seen_subtypes: set[str] = set()

        for p in matching_pool:
            pname = _norm_text(p.get("name", ""))
            pcat = _norm_text(p.get("category") or "")
            st = _product_subtype(pname)
            if st in seen_subtypes:
                continue
            if p.get("in_last_order") and len(matching_pool) > count:
                skipped_last_order.append(p["name"].split(" - ")[0])
                continue
            is_big_freezer = "surgele" in pcat or any(k in pname for k in ("1kg", "600g", "x10", "x50", "cordon bleu", "cordons bleus", "kebab", "nugget"))
            if "viande" in norm_item and is_big_freezer:
                if freezer_taken >= max_freezer_packs:
                    continue
                freezer_taken += 1

            seen_subtypes.add(st)
            selected.append(p)
            if len(selected) >= count:
                break

        if len(selected) < count:
            for p in matching_pool:
                st = _product_subtype(_norm_text(p.get("name", "")))
                if st not in seen_subtypes:
                    seen_subtypes.add(st)
                    selected.append(p)
                    if len(selected) >= count:
                        break

        if skipped_last_order and arbitrations_out is not None:
            uniq_l = list(dict.fromkeys(skipped_last_order))[:3]
            arbitrations_out.append(
                f"🔄 Rotation sur '{generic_item}' : {', '.join(uniq_l)} déjà acheté(s) à la dernière commande ({last_order_date}), écarté(s) pour varier."
            )

        return selected

    def detect_forgotten_staples(self, shopping_plan: list[dict], stats: dict, max_reminders: int = 6) -> list[dict]:
        products = stats.get("products", [])
        orders_count = max(1, int(stats.get("orders_count", 1)))
        planned_ids = {str(s.get("preferred_leclerc_id") or "").strip() for s in shopping_plan if s.get("preferred_leclerc_id")}
        planned_subtypes = {_product_subtype(_norm_text(s.get("search_query", ""))) for s in shopping_plan}
        planned_queries_norm = [_norm_text(s.get("search_query", "")) for s in shopping_plan]

        reminders: list[dict] = []
        seen_subtypes: set[str] = set()

        for p in products:
            lid = str(p.get("leclerc_id") or "").strip()
            pname = p.get("name", "")
            pnorm = _norm_text(pname)
            st = _product_subtype(pnorm)
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
                freq_pct = round((occ / orders_count) * 100)
                if avg_int and days_last is not None and days_last >= avg_int:
                    reason = f"Acheté dans {occ}/{orders_count} commandes ({freq_pct}%), cycle ~{avg_int}j dépassé (dernier achat il y a {days_last}j)"
                else:
                    reason = f"Produit pilier acheté dans {occ}/{orders_count} commandes ({freq_pct}%), dernier achat il y a {days_last}j"
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

    def _get_card_in_cart_qty(self, card) -> int:
        try:
            return int(
                card.evaluate("""
                    (cardEl) => {
                        const isVis = (el) => {
                            if (!el) return false;
                            const s = window.getComputedStyle(el);
                            if (s.display === 'none' || s.visibility === 'hidden' || parseFloat(s.opacity) === 0) return false;
                            const r = el.getBoundingClientRect();
                            return r.width > 2 && r.height > 2;
                        };
                        const lessBtn = cardEl.querySelector('a.aWCRS310_Less, button.aWCRS310_Less, [class*="WCRS310"][class*="Less"], [class*="WCRS310"][class*="Moins"]');
                        if (!lessBtn || !isVis(lessBtn)) return 0;
                        const container = lessBtn.parentElement || cardEl;
                        for (const sp of container.querySelectorAll('span, p, div')) {
                            const t = (sp.innerText || '').trim();
                            if (/^\\d+$/.test(t)) return parseInt(t, 10);
                        }
                        return 1;
                    }
                """)
                or 0
            )
        except Exception:
            return 0

    def add_product_by_query(self, query: str, qty: int = 1, preferred_leclerc_id: str | None = None) -> dict:
        clean_query = query.strip()
        safe_qty = max(1, int(qty or 1))
        page = self._get_page()

        slug_part = self.magasin_slug or "magasin-125401-125401-Vandoeuvre-les-Nancy"
        url = f"https://{self.fd_shard}-courses.leclercdrive.fr/{slug_part}/recherche.aspx?TexteRecherche={quote_plus(clean_query)}"
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_timeout(2000)
        self._dismiss_cookies(page)

        cards = page.locator("li.liWCRS310_Product")
        total = min(cards.count(), 8)
        if total == 0:
            return {"query": clean_query, "status": "introuvable_sur_le_drive", "reason": f"Aucun résultat pour '{clean_query}'", "qty": 0}

        stats = self.compute_stats(120)
        id_counts = {str(p["leclerc_id"]): p["occurrences"] for p in stats["products"] if p.get("leclerc_id")}
        candidates = []

        for i in range(total):
            card = cards.nth(i)
            raw_text = card.inner_text()
            lower_text = raw_text.lower()
            available = not ("indisponible" in lower_text or "rupture" in lower_text)
            leclerc_id = card.get_attribute("iidproduit") or ""
            stitre1 = (card.get_attribute("stitre1") or "").strip()
            stitre2 = (card.get_attribute("stitre2") or "").strip()
            title_el = card.locator("a.aWCRS310_Product, .pWCRS310_Desc, h2, h3").first

            if stitre1:
                title = f"{stitre1} - {stitre2}" if stitre2 else stitre1
            elif title_el.count() > 0:
                title = title_el.inner_text().replace("\n", " ").strip()
            else:
                title = "Inconnu"
            if "Ajouter au panier" in title:
                title = title.split("Ajouter au panier")[0].strip()

            price_el = card.locator("[class*='Prix'], [class*='prix']").first
            price = price_el.inner_text().replace("\n", " ").strip() if price_el.count() > 0 else ""

            candidates.append({
                "index": i,
                "leclerc_id": leclerc_id,
                "title": title[:110],
                "price": price,
                "available": available,
                "history_purchases": id_counts.get(leclerc_id, 0),
            })

        q_tokens = _significant_tokens(clean_query)
        target_idx = None

        if preferred_leclerc_id:
            for c in candidates:
                if c["available"] and str(c["leclerc_id"]).strip() == str(preferred_leclerc_id).strip():
                    if _candidate_token_hits(c["title"], q_tokens)[0]:
                        target_idx = c["index"]
                        break

        if target_idx is None:
            relevant = []
            for c in candidates:
                if not c["available"]:
                    continue
                is_rel, hits = _candidate_token_hits(c["title"], q_tokens)
                if is_rel:
                    relevant.append((c, hits))
            if relevant:
                best_c, _ = max(relevant, key=lambda x: (x[1], 1 if x[0]["history_purchases"] > 0 else 0, x[0]["history_purchases"], -x[0]["index"]))
                target_idx = best_c["index"]

        if target_idx is None:
            return {
                "query": clean_query,
                "status": "introuvable_sur_le_drive",
                "reason": f"Aucun produit correspondant à '{clean_query}' trouvé sur E.Leclerc Drive (aucun produit de remplacement ajouté).",
                "qty": 0,
            }

        chosen = candidates[target_idx]
        card = cards.nth(target_idx)
        before_state = self.get_cart_and_auth_state(page)
        before_card_qty = self._get_card_in_cart_qty(card)

        # Pause humaine de lecture de la page (~3.5 à 5s) pour éviter le bridage de l'API E.Leclerc
        page.wait_for_timeout(int(random.uniform(3400, 4800)))
        card.evaluate("el => el.scrollIntoView({block: 'center', inline: 'nearest'})")
        page.wait_for_timeout(int(random.uniform(800, 1400)))

        clicks_done = 0
        for unit_idx in range(safe_qty):
            unit_before = self.get_cart_and_auth_state(page)
            card_qty_before = self._get_card_in_cart_qty(card)
            unit_confirmed = False

            for attempt in range(2):
                found = card.evaluate("""
                    (cardEl) => {
                        cardEl.querySelectorAll('[data-leclerc-add-target]').forEach(e => e.removeAttribute('data-leclerc-add-target'));
                        const isVis = (el) => {
                            if (!el) return false;
                            const s = window.getComputedStyle(el);
                            if (s.display === 'none' || s.visibility === 'hidden' || parseFloat(s.opacity) === 0) return false;
                            const r = el.getBoundingClientRect();
                            return r.width > 2 && r.height > 2;
                        };
                        const sels = [
                            'a.aWCRS310_More', 'button.aWCRS310_More', 'span.spanWCRS310_More',
                            'a.aWCRS310_Plus', 'button.aWCRS310_Plus', 'a.aWCRS310_Add', 'a.aWCRS310_Ajouter',
                            'a.aWCRS310_Panier',
                            '[class*="WCRS310"][class*="More"]:not([class*="Variete"])',
                            '[class*="WCRS310"][class*="Plus"]:not([class*="Variete"])',
                            '.divWCRS310_Panier a:not([class*="Less"]):not([class*="Moins"])',
                            '.pWCRS310_Panier a:not([class*="Less"]):not([class*="Moins"])',
                            '[class*="WCRS310"][class*="Panier"]:not([class*="List"]) a:not([class*="Less"]):not([class*="Moins"])'
                        ];
                        for (const sel of sels) {
                            for (const node of cardEl.querySelectorAll(sel)) {
                                const txt = (node.innerText || '').trim();
                                if (txt === '-' || txt === '–' || txt === '−') continue;
                                const sig = (node.className + ' ' + (node.id || '') + ' ' + (node.title || '') + ' ' + txt).toLowerCase();
                                if (isVis(node) && !sig.includes('less') && !sig.includes('moins') && !sig.includes('list') && !node.classList.contains('aWCRS310_Product')) {
                                    node.setAttribute('data-leclerc-add-target', '1');
                                    return true;
                                }
                            }
                        }
                        return false;
                    }
                """)
                if not found:
                    break
                try:
                    card.locator("[data-leclerc-add-target='1']").first.click(timeout=4000)
                except Exception:
                    card.evaluate("el => { const t = el.querySelector('[data-leclerc-add-target=\"1\"]'); if (t) t.click(); }")
                clicks_done += 1
                page.wait_for_timeout(1200)

                for _ in range(7):
                    curr = self.get_cart_and_auth_state(page)
                    curr_card_q = self._get_card_in_cart_qty(card)
                    if curr_card_q > card_qty_before or curr["cart_count"] > unit_before["cart_count"] or curr["cart_total"] != unit_before["cart_total"]:
                        unit_confirmed = True
                        break
                    page.wait_for_timeout(600)

                if unit_confirmed:
                    break
                if attempt == 0:
                    page.wait_for_timeout(3500)

            if unit_idx < safe_qty - 1:
                page.wait_for_timeout(int(random.uniform(2200, 3200)))

        after_state = self.get_cart_and_auth_state(page)
        after_card_qty = self._get_card_in_cart_qty(card)
        page.wait_for_timeout(int(random.uniform(2500, 4000)))

        return {
            "query": clean_query,
            "selected_product": chosen["title"],
            "leclerc_id": chosen["leclerc_id"],
            "price": chosen["price"],
            "status": "added" if clicks_done > 0 else "add_button_not_found",
            "qty": safe_qty if clicks_done > 0 else 0,
            "cart_verified": (after_card_qty > before_card_qty or after_state["cart_count"] > before_state["cart_count"]),
            "cart_count_after": after_state["cart_count"],
            "cart_total_after": after_state["cart_total"],
        }
