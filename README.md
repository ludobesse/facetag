# FaceTag

Application de bureau qui **reconnaît les visages dans vos photos JPEG**, vous aide à les nommer
avec les noms de vos contacts, puis **écrit les noms dans les photos elles-mêmes** (métadonnées
XMP standard, lisibles par digiKam et d'autres logiciels).

Tout se passe sur votre ordinateur : les photos, les visages et leurs empreintes ne sont jamais
envoyés en ligne.

## Fonctionnalités

- Détection et reconnaissance des visages en local (modèles YuNet et SFace d'OpenCV), adaptée
  aux grandes collections (photos sur un NAS : environ 1 h estimée pour 50 000 photos, d'après un
  banc d'essai sur 500 photos avec un PC récent).
- Noms issus de vos contacts : **fichier exporté** (.vcf / .csv) ou **connexion directe à Google
  Contacts**.
- Regroupement des visages inconnus qui se ressemblent : on nomme tout un groupe en un clic, avec
  des **noms suggérés** pour chaque groupe.
- **Apprentissage continu** : chaque nom confirmé, refusé ou corrigé améliore les suggestions
  suivantes.
- Gestion des **fausses détections** (statues, affiches, motifs…) et des groupes mélangés.
- Écriture des noms dans les photos, avec **simulation préalable**.
- Avec la connexion directe : création de contacts Google et choix d'un visage comme photo de
  contact.

## Installation (Windows)

Prérequis : [Python](https://www.python.org/downloads/) 3.10 ou plus récent, et
[Git](https://git-scm.com/) pour récupérer le code.

```powershell
git clone https://github.com/ludobesse/facetag.git
cd facetag
py -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python -m facetag.app
```

Raccourci sur le bureau, sans fenêtre de console (à lancer depuis le dossier `facetag`) :

```powershell
$s = (New-Object -ComObject WScript.Shell).CreateShortcut("$env:USERPROFILE\Desktop\FaceTag.lnk")
$s.TargetPath = "$PWD\.venv\Scripts\pythonw.exe"
$s.Arguments = "-m facetag.app"
$s.WorkingDirectory = "$PWD"
$s.Save()
```

Au premier lancement, FaceTag télécharge les modèles de visages depuis
[OpenCV Zoo](https://github.com/opencv/opencv_zoo) (empreintes SHA-256 vérifiées).

## Premier lancement

### 1. ExifTool

ExifTool est nécessaire pour écrire les noms dans les photos (le reste fonctionne sans). S'il est
absent, FaceTag propose de le **télécharger et l'installer** : archive officielle, empreinte SHA-256
vérifiée avec celle publiée sur [exiftool.org](https://exiftool.org/), installation sans droits
administrateur. Vous pouvez aussi indiquer un `exiftool.exe` existant dans **⚙ Paramètres**.

### 2. Vos contacts (⚙ Paramètres)

Les Paramètres s'ouvrent automatiquement au premier lancement. Deux méthodes :

**Méthode simple : importer un fichier** (aucun compte développeur)
1. Sur [contacts.google.com](https://contacts.google.com/) : *Exporter* → **vCard** (ou Google CSV).
2. Dans FaceTag : **Importer un fichier de contacts (.vcf / .csv)…**

Les noms et les photos de profil contenues dans le fichier sont importés. Cela fonctionne aussi
avec les exports iPhone et Outlook. C'est une copie : refaites un export pour ajouter de nouveaux
contacts.

**Connexion directe** (synchronisation, création de contacts, photos de contact)

Chaque utilisateur crée son propre identifiant Google, gratuitement (environ 5 minutes) :
1. Ouvrir [Google Cloud Console](https://console.cloud.google.com/) et créer un projet.
2. *API et services → Bibliothèque* : activer **People API**.
3. *Écran de consentement OAuth* (ou *Google Auth Platform → Audience*) : type **Externe**, puis
   ajouter votre adresse Gmail comme **utilisateur test**.
4. *Identifiants → Créer des identifiants → ID client OAuth* : type **Application de bureau**,
   puis télécharger le fichier JSON.
5. Dans FaceTag : **Importer le fichier JSON…**, puis **Se connecter**.

**Se connecter** donne un accès en lecture seule. **Autoriser aussi l'écriture** est nécessaire
pour créer des contacts et changer leur photo. **Se déconnecter** révoque l'autorisation chez
Google et supprime le jeton local. En mode « Test », Google redemande l'autorisation après 7 jours :
FaceTag rouvre alors simplement le navigateur.

## Utilisation

### 1. Détecter les visages

**Détecter les visages dans un dossier…** : choisissez un dossier (disque local, lecteur réseau
ou chemin `\\NAS\photo`).
- Seules les photos nouvelles ou modifiées sont analysées.
- **Arrêter** à tout moment : la progression est conservée, l'analyse reprend au même endroit.
- Les dossiers techniques Synology (`@eaDir`, `#recycle`) sont ignorés.

### 2. Nommer

| Vue (à gauche) | Usage |
|---|---|
| **Groupes d'inconnus** | visages qui se ressemblent. Sélectionnez-les (Ctrl+A), tapez un nom (auto-complétion sur vos contacts), Entrée. Si FaceTag reconnaît le groupe, il affiche « → Nom ? » et propose des boutons « Nom (score) » |
| **À confirmer** | suggestions par personne, toutes présélectionnées : désélectionnez les erreurs (Ctrl+clic), « Pas cette personne », puis **Entrée** pour confirmer le reste |
| **Inconnus isolés** | visages sans ressemblance avec d'autres, à nommer un par un |
| **Personnes** | visages confirmés : renommer, « Retirer le nom », photo de contact |
| **Fausses détections probables** | ressemblent à une fausse détection déjà signalée : « Pas un visage » ou « C'est bien un visage » |
| **Ignorés** / **Fausses détections** | « Rétablir » si besoin |

Pour démarrer, nommez d'abord quelques **groupes d'inconnus** : les suggestions apparaissent
ensuite d'elles-mêmes.

Un nom absent de vos contacts : FaceTag propose de le créer dans Google Contacts (avec la
connexion directe) ou seulement dans FaceTag. Une icône bleue signale les personnes liées à
Google Contacts.

### 3. Corriger

- **Pas cette personne** : la suggestion n'est plus proposée pour ce visage.
- **Pas un visage** (Maj+Suppr) : fausse détection (statue, affiche, reflet…), plus jamais
  proposée, même si la photo est réanalysée.
- **Ignorer** (Suppr) : un vrai visage que vous ne voulez pas nommer.
- **Scinder ce groupe** : coupe un groupe mélangé en deux sous-groupes ; répétable ; la coupure
  est mémorisée.
- **Retirer du groupe** : sort les visages sélectionnés d'un groupe.

**Apprentissage continu** : 1,5 s après chacune de ces actions, les suggestions, les groupes et
leurs noms suggérés sont recalculés en arrière-plan. La barre d'état indique ce qui a changé.

### 4. Écrire les noms dans les photos

**Écrire les métadonnées…** commence par une **simulation** (rien n'est modifié), puis écrit sur
confirmation. Vous pouvez limiter l'écriture à un dossier : **testez d'abord sur un dossier de
copies**.

Écrit dans chaque photo :
- `XMP-mwg-rs:RegionInfo` (norme MWG 2.0) : zone « Face » et nom pour chaque visage confirmé, en
  tenant compte de l'orientation EXIF de la photo ;
- `XMP-iptcExt:PersonInImage` : la liste des noms (champ IPTC standard, utile pour la recherche).

Les zones déjà présentes (par exemple écrites par digiKam) sont conservées, sauf celles qui
recouvrent un visage connu de FaceTag, qui sont remplacées. La date de modification des fichiers
est préservée. Aucune copie de sauvegarde n'est créée : gardez une sauvegarde de vos photos.

Vérifier une photo :
`exiftool -RegionName -RegionType -PersonInImage photo.jpg`

### Raccourcis

**Entrée** confirmer · **Suppr** ignorer · **Maj+Suppr** pas un visage · **Ctrl+N** saisir un nom ·
**Ctrl+A** tout sélectionner · **double-clic** ouvrir la photo

## Réglages

Dans **⚙ Paramètres → Reconnaissance des visages** : **Utiliser les photos de profil des
contacts comme point de départ** (désactivé par défaut). Activé, la photo de profil d'un contact
sert à proposer son nom avant que vous ayez nommé un de ses visages. Ces photos sont souvent
petites (~96 px) ou anciennes : attendez-vous à plus d'erreurs. Dès qu'une personne a un visage
confirmé, sa photo de profil n'est plus utilisée.

Seuils (en tête de `facetag/match.py`, valeurs de départ à ajuster selon vos résultats) :
suggestion à partir d'une similarité de 0,40, regroupement à partir de 0,50, nom suggéré pour un
groupe à partir de 0,30.

## Données et confidentialité

Tout est enregistré dans `%LOCALAPPDATA%\FaceTag\`, jamais dans le dossier du programme :

| Fichier / dossier | Contenu |
|---|---|
| `facetag.db` | base SQLite : photos analysées, visages, empreintes, noms |
| `thumbs\` | vignettes des visages |
| `contact_photos\` | photos de profil des contacts |
| `models\` | modèles YuNet et SFace |
| `exiftool\` | ExifTool, s'il a été installé par FaceTag |
| `google_client.json`, `google_token.json` | identifiant et autorisation Google (connexion directe) |
| `config.json` | réglages |

Connexions réseau effectuées par FaceTag : téléchargement des modèles (GitHub) et d'ExifTool
(exiftool.org, SourceForge), et, si vous l'utilisez, l'API Google Contacts. Aucune photo ni
donnée de visage n'est envoyée.

## Ligne de commande (avancé)

```powershell
python -m facetag.scan "\\NAS\photo"            # détection (--rescan : tout réanalyser)
python -m facetag.scan "\\NAS\photo" --bench 500 # banc d'essai de vitesse
python -m facetag.match                          # suggestions et groupes
python -m facetag.write                          # simulation d'écriture
python -m facetag.write --apply --folder "\\NAS\photo\Test"   # écrire (--backup : garder *_original)
python -m facetag.status                         # état de la base (lecture seule)
python -m facetag.check photos --n 60            # compare des réglages de détection (images dans debug\)
```

## Limites connues

- Seuls les fichiers **JPEG** sont analysés.
- Les seuils de suggestion et de regroupement sont des valeurs de départ, à ajuster.
- Développé et utilisé sous **Windows**. Le code est en Python et Qt, mais l'installation
  automatique d'ExifTool et les raccourcis sont propres à Windows.

## Pour qui redistribue FaceTag

Un identifiant OAuth « Application de bureau » placé dans `facetag/default_google_client.json`
permet aux utilisateurs de cliquer directement sur **Se connecter**. Les accès aux contacts sont
des autorisations « sensibles » pour Google : sans validation de l'application par Google, un
écran « application non validée » s'affiche et l'application est limitée à 100 nouveaux
utilisateurs. Ce fichier est exclu du dépôt par `.gitignore`.

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
