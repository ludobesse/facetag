# FaceTag

Reconnaissance de visages **locale** pour photos JPEG, avec noms issus de Google Contacts
et écriture des noms dans les métadonnées (XMP MWG). Aucune donnée faciale ne quitte votre PC.

## Utilisation au quotidien

```powershell
.venv\Scripts\activate
python -m facetag.app                     # tout se fait depuis l'application
python -m facetag.scan "\\NAS\photo"     # (ligne de commande) analyser les nouvelles photos
python -m facetag.match                   # calculer les suggestions de noms
python -m facetag.status                  # où en est la base (lecture seule)
```

## État

| Étape | Contenu | Commande |
|---|---|---|
| 1 | Import Google Contacts (noms + photos de profil) | `python -m facetag.contacts` |
| 2 | Scan des photos : détection YuNet + empreintes SFace | `python -m facetag.scan` |
| 3 | Suggestions de noms + groupes d'inconnus | `python -m facetag.match` |
| 4 | Application de bureau : confirmer / nommer / ignorer | `python -m facetag.app` |
| 5 | Écriture XMP MWG via ExifTool | bouton dans l'application, ou `python -m facetag.write` |

## Installation (Windows)

```powershell
git clone <adresse du dépôt> facetag
cd facetag
py -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

### ExifTool (nécessaire pour l'étape 5)

Au démarrage, FaceTag vérifie qu'ExifTool est disponible et, sinon, propose de le
**télécharger et l'installer** (Windows) : version lue sur https://exiftool.org/ver.txt,
archive officielle, empreinte SHA-256 vérifiée avec `checksums-<version>.txt` publié sur
exiftool.org, installation dans `%LOCALAPPDATA%\FaceTag\exiftool\`. Aussi disponible dans
**⚙ Paramètres → ExifTool → Installer…**. Installation manuelle :

1. Télécharger `exiftool-XX_64.zip` sur https://exiftool.org/
2. Dans le zip, renommer `exiftool(-k).exe` en `exiftool.exe`.
3. Copier `exiftool.exe` **et** le dossier `exiftool_files` dans `tools\exiftool\` (dans le dossier
   du programme), **ou** n'importe où et indiquer le chemin dans **⚙ Paramètres → ExifTool**.

## Contacts : deux méthodes

**Méthode simple (recommandée pour démarrer)** — aucun compte développeur :
sur https://contacts.google.com/ → *Exporter* → **vCard** (ou Google CSV), puis dans FaceTag
**⚙ Paramètres → Importer un fichier de contacts (.vcf / .csv)…**. Les noms (et photos de
profil présentes dans le fichier) sont importés. C'est une copie : refaites un export pour
ajouter de nouveaux contacts. Fonctionne aussi avec les exports iPhone/Outlook (vCard).

**Connexion directe** — synchronisation, création de contacts et photo de contact depuis FaceTag.
Nécessite un identifiant OAuth (ci-dessous), sauf si la version que vous utilisez en fournit un.

### Pour qui publie FaceTag : identifiant fourni avec l'application

Si le fichier `facetag/default_google_client.json` (ID client OAuth « Application de bureau »)
est présent, les utilisateurs n'ont qu'à cliquer sur **Se connecter**. Points à connaître :
- Google considère les applications de bureau comme des clients publics : le « secret » n'en
  est pas vraiment un, mais le projet Google, ses quotas et sa page de consentement sont ceux
  de l'éditeur.
- Les accès contacts (`contacts`, `contacts.readonly`) sont des *scopes sensibles* : sans
  validation par Google, l'écran « application non validée » s'affiche et l'application est
  limitée à **100 nouveaux utilisateurs**. Au-delà : demande de validation à Google
  (politique de confidentialité, page d'accueil, domaine vérifié…).

## Connexion directe à Google Contacts (une seule fois)


Tout se fait dans **⚙ Paramètres** (ouvert automatiquement au premier lancement), qui affiche
aussi ce guide. Chaque utilisateur crée **son propre** identifiant Google, gratuit : aucun
identifiant n'est fourni avec le programme.

1. Ouvrir https://console.cloud.google.com/ et créer un projet.
2. *API et services → Bibliothèque* : activer **People API**.
3. *Écran de consentement OAuth* (ou *Google Auth Platform → Audience*) : type **Externe**,
   et ajouter votre adresse Gmail comme **utilisateur test**.
4. *Identifiants → Créer des identifiants → ID client OAuth* : type **Application de bureau**,
   puis télécharger le fichier JSON.
5. Dans FaceTag : **⚙ Paramètres → Importer le fichier JSON…**, puis **Se connecter**.

- Le fichier est copié dans `%LOCALAPPDATA%\FaceTag\google_client.json`, jamais dans le dossier
  du programme (rien de personnel dans le dépôt).
- **Se connecter** = lecture seule. **Autoriser aussi l'écriture** = création de contacts et
  photos de contact. **Se déconnecter** révoque l'autorisation chez Google
  (`https://oauth2.googleapis.com/revoke`) et supprime le jeton local.
- En mode « Test », Google fait expirer l'autorisation après 7 jours : FaceTag rouvre alors
  simplement le navigateur.
- Compatibilité : un `client_secret*.json` posé à côté du programme est encore accepté.

Données créées dans `%LOCALAPPDATA%\FaceTag\` : `facetag.db` (base), `contact_photos\`,
`thumbs\` (vignettes), `models\` (YuNet, SFace), `google_client.json`, `google_token.json`,
`config.json` (réglages).

## Étape 2 : scanner les photos du NAS

```powershell
python -m facetag.scan --contacts                    # visages de référence (photos de profil)
python -m facetag.scan "\\NAS\photo" --bench 500  # banc d'essai
python -m facetag.scan "\\NAS\photo"              # scan complet, interruptible (Ctrl+C) et reprenable
```

- **Incrémental** : seules les photos nouvelles, modifiées, ou analysées avec d'anciens réglages
  sont (ré)analysées.
- Les dossiers techniques Synology (`@eaDir`, `#recycle`) sont ignorés.
- Les visages **confirmés ou ignorés** sont conservés si une photo est réanalysée.
- `--rescan` : tout réanalyser · `--io-threads N` : lectures réseau parallèles (défaut 4).

## Étape 3 : suggestions

`python -m facetag.match` (ou bouton « Recalculer les suggestions » dans l'application).

- Compare chaque visage inconnu aux visages que vous avez **confirmés**.
- Option (**⚙ Paramètres → Reconnaissance des visages**, désactivée par défaut, ou
  `--photos-contacts`) : les photos de profil servent de point de départ, seulement pour les
  personnes sans visage confirmé. Peu fiable : photos souvent petites (~96 px) ou anciennes.
- Pour démarrer sans photos de profil : nommez d'abord les **groupes d'inconnus**.
- Suggestion si similarité ≥ 0.40 et avance ≥ 0.05 sur la 2e personne la plus proche.
- Les inconnus qui se ressemblent (≥ 0.50) sont regroupés : on nomme un groupe d'un coup.
- Une suggestion refusée (« Pas cette personne ») n'est plus reproposée pour ce visage.
- Seuils de départ, à ajuster selon les résultats (en tête de `facetag\match.py`).

**Plus vous confirmez, meilleures sont les suggestions** : relancez le calcul régulièrement.

## Étape 4 : application

`python -m facetag.app`

Barre du haut :
- **Détecter les visages dans un dossier…** : choisissez un dossier ; seules les photos nouvelles ou
  modifiées sont analysées, puis les suggestions sont recalculées. Bouton « Arrêter » à tout moment.
- **Recalculer les suggestions** · **Écrire les métadonnées…**

Nom absent de Google Contacts : l'application propose **« Créer aussi dans Google Contacts »**
(prénom/nom à vérifier) ou « Seulement dans FaceTag ». Une personne « hors Google » peut être
ajoutée plus tard avec le bouton **« Créer le contact Google »** (vue Personnes).
La première création ouvre le navigateur pour autoriser l'**écriture** dans vos contacts
(l'import, lui, reste en lecture seule).

Photo de contact : dans la vue **Personnes**, sélectionnez **un** visage puis
**« 📷 Photo du contact Google »** : aperçu du recadrage, puis remplacement de la photo du contact.

**Apprentissage continu** : 1,5 s après chacune de vos actions (nommer, confirmer, « Pas cette
personne », « Pas un visage », scinder…), les suggestions, les groupes et leurs noms suggérés sont
recalculés en arrière-plan. La barre d'état indique ce qui a changé ; cliquez sur une catégorie pour
voir les nouveautés. Le bouton « Recalculer les suggestions » reste disponible.

**Fausses détections** : « Pas un visage » (Maj+Suppr) les range dans « Fausses détections ».
Les visages inconnus qui leur ressemblent fortement (même statue, même affiche…) apparaissent dans
« Fausses détections probables » pour vérification : « Pas un visage » ou « C'est bien un visage ».

| Vue (à gauche) | Usage |
|---|---|
| À confirmer | suggestions par personne, toutes pré-sélectionnées : désélectionnez les erreurs (Ctrl+clic), « Pas cette personne », puis **Entrée** pour confirmer le reste |
| Groupes d'inconnus | sélectionnez (Ctrl+A), tapez un nom (auto-complétion sur vos contacts), Entrée |
| Groupes d'inconnus mélangés | **« Scinder ce groupe »** : coupe en deux sous-groupes (répétable) ; la coupure est mémorisée. **« Retirer du groupe »** : sort les visages sélectionnés |
| Groupes : noms suggérés | « → Nom ? » dans la liste quand un groupe ressemble à une personne connue ; dans le groupe, boutons « Nom (score) » : un clic nomme la sélection, ou tout le groupe si rien n'est sélectionné |
| Inconnus isolés | idem, visage par visage |
| Personnes | visages confirmés ; « Retirer le nom » ou renommer |
| Ignorés | « Rétablir » si besoin |

Raccourcis : **Entrée** confirmer · **Suppr** ignorer · **Ctrl+N** saisir un nom ·
**double-clic** ouvrir la photo. Un nom absent de Google Contacts peut être créé (confirmation demandée).

## Étape 5 : écriture dans les photos

Bouton « Écrire les métadonnées… » : **simulation d'abord**, puis écriture sur confirmation.
En ligne de commande :

```powershell
python -m facetag.write                                        # simulation
python -m facetag.write --apply --folder "\\NAS\photo\Test" # tester sur un dossier
python -m facetag.write --apply                                # tout écrire
```

Écrit dans chaque photo :
- `XMP-mwg-rs:RegionInfo` : zone « Face » + nom pour chaque visage confirmé. Les zones sont
  converties vers l'image **stockée** (norme MWG 2.0), en tenant compte de l'orientation EXIF.
- `XMP-iptcExt:PersonInImage` : liste des noms (champ IPTC standard).

Zones existantes (ex. digiKam) : conservées, sauf celles qui recouvrent un visage connu de FaceTag
(remplacées). Date de modification du fichier préservée (`-P`). Pas de copie `_original`
(sauf `--backup`). **Testez d'abord sur un dossier de copies.**

Vérifier une photo : `tools\exiftool\exiftool.exe -RegionName -RegionType -PersonInImage photo.jpg`

## Diagnostic

```powershell
python -m facetag.status                  # état de la base (lecture seule)
python -m facetag.check photos --n 60     # compare des réglages de détection
python -m facetag.check contacts          # photos de contacts sans visage + planche-contact
```

Images de diagnostic dans `debug\` (vert = visage retenu, orange = trop petit).

## Licence

FaceTag est un logiciel libre distribué sous licence **GNU GPL v3** (voir [`LICENSE`](LICENSE)) :
vous pouvez l'utiliser, l'étudier, le modifier et le redistribuer ; toute version modifiée
redistribuée doit l'être sous la même licence, avec son code source.

Composants tiers (non inclus dans ce dépôt, téléchargés ou installés séparément) :

| Composant | Rôle | Licence |
|---|---|---|
| [YuNet](https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet) | détection des visages | MIT |
| [SFace](https://github.com/opencv/opencv_zoo/tree/main/models/face_recognition_sface) | empreintes de visages | Apache 2.0 |
| [OpenCV](https://opencv.org/) | traitement d'image | Apache 2.0 |
| [PySide6 / Qt](https://doc.qt.io/qtforpython/) | interface | LGPL v3 |
| [ExifTool](https://exiftool.org/) | écriture des métadonnées | Perl (Artistic / GPL) |
| Bibliothèques Google API (Python) | accès à Google Contacts | Apache 2.0 |

