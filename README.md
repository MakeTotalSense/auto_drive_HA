# 🛒 E.Leclerc Drive AI — Add-on Home Assistant (avec Ingress)

Module complémentaire (Add-on) officiel pour **Home Assistant** (compatible **Raspberry Pi 4 / 5 (64-bit)**, Home Assistant Yellow/Green, PC x86_64, VM Proxmox/Unraid).

L'Add-on tourne dans son propre conteneur Docker **isolé de Home Assistant Core**, avec son propre Chromium, Node.js et Python pré-installés. Il s'affiche directement **dans la barre latérale de Home Assistant (Ingress)** et met à jour automatiquement vos capteurs et notifications HA.

---

## 🌟 Fonctionnalités intelligentes

1. **Rotation intelligente des viandes & Anti-saturation du congélateur** :
   - Évite d'empiler plusieurs gros formats surgelés (1kg de steaks + 1kg de cordons bleus + kebab) : limite à 1 seul gros format surgelé et complète avec des viandes fraîches (filet de poulet, blanc de poulet, boulettes).
   - Analyse la toute dernière commande pour mettre de côté les viandes déjà achetées et varier vos repas.
2. **Saisonnalité des fruits et légumes en France** :
   - Adapte automatiquement la sélection en fonction de la saison actuelle en France (ex. automne/hiver : évite les tomates, concombres, courgettes et abricots d'été, et privilégie choux, poireaux, carottes, salades, pommes, poires, clémentines).
3. **Détection du mode de courses** :
   - **Courses rapprochées / Appoint** (≤ 5j depuis la dernière commande) : quantités réduites, aucun doublon avec la dernière commande.
   - **Courses régulières** (6 à 20j) : rotation normale.
   - **Giga courses / Frigo à sec** (≥ 21j) : panier plus garni, 2 surgelés autorisés, quantités augmentées sur vos produits piliers.
4. **Rappels d'oublis habituels** :
   - Détecte vos produits phares dans l'historique dont le cycle de rachat habituel est dépassé et que vous avez oublié de noter aujourd'hui.
5. **Recherche stricte & Zéro fausse substitution** :
   - Si un article est introuvable sur le Drive (ex: *Levure sèche*), il n'est **jamais** remplacé par un produit sans rapport. Il est listé clairement dans les articles introuvables.
6. **Interface Web Ingress & Déblocage Anti-Captcha** :
   - Tableau de bord en direct dans Home Assistant.
   - Visualisation de ce que voit le navigateur en direct (capture d'écran).
   - Prise en charge du code OTP à 6 chiffres par e-mail en 1 clic.
   - Import de session facile si un Captcha DataDome survient.

---

## 🚀 Installation dans Home Assistant (en 3 clics)

1. Dans votre Home Assistant, allez dans **Paramètres** > **Modules complémentaires** > **Boutique de modules complémentaires** (en bas à droite).
2. Cliquez sur les **trois petits points en haut à droite** > **Dépôts**.
3. Ajoutez l'URL suivante :
   ```text
   https://github.com/MakeTotalSense/auto_drive_HA
   ```
4. Cliquez sur **Ajouter** puis fermez la fenêtre.
5. L'add-on **E.Leclerc Drive AI** apparaît maintenant dans la boutique !
6. Cliquez dessus puis sur **Installer**.
7. Cochez **Afficher dans la barre latérale** puis cliquez sur **Démarrer**.
8. Cliquez sur **Ouvrir l'interface utilisateur web** (ou accédez-y depuis le menu latéral de Home Assistant).

---

## ⚙️ Configuration simple (depuis l'interface de l'Add-on)

Vous pouvez configurer l'Add-on soit depuis l'onglet **Configuration** de l'Add-on dans Home Assistant, soit directement depuis l'onglet **⚙️ Configuration** de sa page Web :

| Paramètre | Description |
| :--- | :--- |
| `leclerc_email` | Votre adresse e-mail E.Leclerc Drive |
| `leclerc_password` | Votre mot de passe E.Leclerc Drive |
| `magasin_slug` | Ville, Code Postal ou URL de votre Drive (laisser vide pour auto-détection) |
| `list_source` | `google_keep` ou `ha_todo` (liste de courses native HA) |
| `google_email` | Votre adresse Gmail (si Google Keep sélectionné) |
| `google_app_password` | Mot de passe d'application Google (16 lettres généré dans votre compte Google) |
| `ai_provider` | `ollama` (qwen2.5:1.5b par défaut) ou `conversation_entity` (agent IA de Home Assistant) |

---

## 🎛️ Intégration domotique Home Assistant

L'Add-on met automatiquement à jour ces entités dans Home Assistant sans aucune configuration manuelle supplémentaire :

- **`sensor.leclerc_drive_panier`** : Montant total en `€`, nombre d'articles, mode de courses et saison en attributs.
- **`sensor.leclerc_drive_rappels_doutes`** : Nombre de manquants, doutes IA et rappels avec les listes complètes dans les attributs.
- **Notification persistante** : Rapport détaillé envoyé dans Home Assistant après chaque commande.

### Déclencher les courses depuis une automatisation ou un bouton HA :
Ajoutez simplement ceci dans votre `configuration.yaml` de Home Assistant :
```yaml
rest_command:
  lancer_courses_leclerc:
    url: "http://localhost:8099/api/run"
    method: POST
```
Vous pouvez ensuite l'appeler dans n'importe quel bouton de tableau de bord ou automatisation :
```yaml
action:
  - service: rest_command.lancer_courses_leclerc
```
