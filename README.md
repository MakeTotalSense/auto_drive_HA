# 🛒 E.Leclerc Drive AI — Intégration Home Assistant (HACS)

Intégration locale pour **Home Assistant** (pensée pour tourner sur un **Raspberry Pi**) qui :
1. Se connecte à votre compte **E.Leclerc Drive** directement depuis l'interface Home Assistant (avec gestion du code de sécurité à 6 chiffres envoyé par e-mail et case *"Appareil de confiance"* cochée automatiquement).
2. **Aspire automatiquement l'historique de vos 15 dernières commandes** dans `history.json` sans aucun clic manuel.
3. Récupère votre liste de courses depuis **Google Keep** (via votre **Mot de passe d'application Google de 16 caractères** converti automatiquement en Master Token) ou depuis une entité **`todo.*` native de Home Assistant**.
4. Utilise au choix une **entité `conversation.*` déjà présente dans votre Home Assistant** (Gemini, OpenAI, Mistral, Ollama HA...) ou **par défaut `qwen2.5:1.5b`** en local via Ollama (`keep_alive: 0` pour libérer la RAM du Raspberry Pi avant d'ouvrir Chromium).

---

## 📦 Structure du dossier `home_assistant`

```text
home_assistant/
├── hacs.json
├── README.md
└── custom_components/
    └── leclerc_drive_ai/
        ├── __init__.py          # Coordinateur, appel conversation.* / Ollama et services HA
        ├── button.py            # Boutons 1-clic (Remplir le panier / Synchroniser l'historique)
        ├── config_flow.py       # Assistant de configuration graphique en 3 étapes dans HA
        ├── const.py             # Constantes de configuration
        ├── leclerc_worker.py    # Moteur Playwright + aspirateur d'historique + Google Keep
        ├── manifest.json        # Déclaration HACS & dépendances
        ├── sensor.py            # Capteurs (Panier actuel, Panier moyen, Statut & dernier rapport)
        ├── services.yaml        # Services leclerc_drive_ai.fill_cart et sync_history
        ├── strings.json
        └── translations/
            └── fr.json          # Interface 100 % en français dans Home Assistant
```

---

## 🚀 Installation dans Home Assistant

1. Copiez le dossier `custom_components/leclerc_drive_ai` dans le dossier `/config/custom_components/` de votre Home Assistant (ou publiez ce dossier `home_assistant/` sur un dépôt GitHub et ajoutez-le dans **HACS > Intégrations > Dépôts personnalisés**).
2. Redémarrez Home Assistant.
3. Allez dans **Paramètres > Appareils et services > + Ajouter une intégration** et cherchez **E.Leclerc Drive AI**.

---

## 🧭 Les 3 étapes de configuration dans l'interface Home Assistant

### Étape 1/3 : Connexion à E.Leclerc Drive & Historique automatique
- Saisissez votre **e-mail** et **mot de passe** E.Leclerc Drive.
- **Magasin** : Vous pouvez laisser le champ **vide** (détection automatique du Drive rattaché à votre compte), saisir votre **Code Postal / Ville** (ex. `54500`, `Lyon`), ou coller l'**URL de votre Drive**.
- **Que faire si E.Leclerc bloque la première connexion avec `captcha_blocked` (DataDome / hCaptcha) ?**
  Comme Home Assistant tourne dans un conteneur *headless* (sans écran), et que les cookies d'authentification E.Leclerc (`.XPRSDRVAUTH`, `AuthPerm`, `clsWMIT013:Cookie_Mitigation`) sont en `HttpOnly` (invisibles pour `document.cookie` dans la Console), vous pouvez transférer votre session complète en **10 secondes sans rien installer** :
  1. Ouvrez votre Drive [leclercdrive.fr](https://www.leclercdrive.fr/) sur votre navigateur habituel (Chrome, Edge, Firefox) et **connectez-vous à votre compte**.
  2. Appuyez sur **`F12`** → onglet **Réseau (Network)** → appuyez sur **`F5`** pour rafraîchir la page.
  3. Faites un **clic droit sur la toute première ligne** (`accueil.aspx`) → **Copier** → **Copier en tant que cURL** (ou *Copier en tant que PowerShell*).
  4. Collez directement (`Ctrl+V`) le tout dans le champ **« Session navigateur »** sur Home Assistant : l'intégration extrait automatiquement tous les cookies (y compris `datadome`, `Cookie_Mitigation` et `.XPRSDRVAUTH`) !
  *(Alternative en 1 clic : avec l'extension navigateur **Cookie-Editor**, cliquez sur **Export → Export as JSON** et collez-le dans le champ).*
- **Si E.Leclerc envoie un code de sécurité à 6 chiffres par e-mail**, une étape intermédiaire s'affiche dans Home Assistant : entrez les 6 chiffres, l'intégration coche automatiquement *"Ne plus demander sur cet appareil"* (`#deviceDeConfiance`).
- Dès la connexion validée, l'intégration **parcourt toute seule votre page "Mes commandes"** en arrière-plan et enregistre vos 15 dernières commandes.

### Étape 2/3 : Google Keep (Mot de passe d'application Google) ou Liste `todo.*`
- **Avec Google Keep** :
  1. Sur votre compte Google, allez dans **Sécurité > Validation en deux étapes > Mots de passe des applications**.
  2. Créez un mot de passe d'application de **16 lettres** (`xxxx xxxx xxxx xxxx`).
  3. Collez-le dans le champ *Mot de passe d'application Google* : la fonction `resolve_google_keep_master_token` (via `gpsoauth`) tente automatiquement de l'échanger contre un Master Token permanent `aas_et/...` (elle accepte aussi directement un token `oauth2_4/...` ou `aas_et/...` si Google bloque `perform_master_login` sur votre compte).
- **Alternative native HA** : Vous pouvez aussi sélectionner directement une entité `todo.shopping_list` de Home Assistant.

### Étape 3/3 : Choix du modèle IA
- Le menu déroulant liste automatiquement :
  - **`Ollama local : qwen2.5:1.5b (Par défaut - Léger Raspberry Pi)`**
  - **Toutes les entités `conversation.*` déjà configurées dans votre Home Assistant** (`conversation.home_assistant`, `conversation.google_generative_ai`, `conversation.ollama`, etc.).

---

## 🎛️ Entités et Services créés dans Home Assistant

- **Boutons (`button.*`)** :
  - `button.remplir_le_panier_e_leclerc` : Lance les courses en 1 clic.
  - `button.synchroniser_l_historique_des_commandes` : Met à jour l'historique des commandes.
- **Capteurs (`sensor.*`)** :
  - `sensor.panier_e_leclerc_actuel` : Montant en `€` et nombre d'articles réellement présents dans votre panier E.Leclerc.
  - `sensor.panier_moyen_historique` : Panier moyen en `€` calculé sur vos commandes.
  - `sensor.statut_courses_e_leclerc` : Statut en temps réel + rapport détaillé dans les attributs.
- **Notification automatique** : À la fin de chaque exécution, une notification Home Assistant récapitule les produits ajoutés et les produits introuvables (non remplacés).
