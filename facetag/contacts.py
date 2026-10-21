"""Étape 1 : import des noms et photos de profil depuis Google Contacts.

Import : lecture seule (scope contacts.readonly), People API people.connections.list.
Création d'un contact (bouton de l'application) : scope contacts, People API people.createContact.
L'autorisation d'écriture n'est demandée qu'au premier contact créé.
Usage : python -m facetag.contacts
"""

import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path

import cv2 as cv
import numpy as np
import requests
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from . import db
from .paths import client_secret_path, contact_photos_dir, token_path

SCOPE_READ = "https://www.googleapis.com/auth/contacts.readonly"
SCOPE_WRITE = "https://www.googleapis.com/auth/contacts"
SCOPES = [SCOPE_READ]
PROJECT_DIR = Path(__file__).resolve().parent.parent


REVOKE_URL = "https://oauth2.googleapis.com/revoke"   # doc Google « OAuth 2.0 for Desktop Apps »


# Identifiant OAuth « Application de bureau » éventuellement fourni par la personne qui publie
# FaceTag (version prête à l'emploi : « Se connecter » suffit). Absent du dépôt par défaut.
BUNDLED_CLIENT = Path(__file__).resolve().parent / "default_google_client.json"


def find_client_secret() -> Path:
    """Identifiant OAuth, par ordre de priorité :
    1. celui importé dans les Paramètres (dossier de données de l'utilisateur) ;
    2. celui fourni avec la version publiée (facetag/default_google_client.json) ;
    3. un client_secret*.json posé à côté du programme (développement)."""
    imported = client_secret_path()
    if imported.exists():
        return imported
    if BUNDLED_CLIENT.exists():
        return BUNDLED_CLIENT
    exact = PROJECT_DIR / "client_secret.json"
    if exact.exists():
        return exact
    found = sorted(PROJECT_DIR.glob("client_secret*.json"))
    return found[0] if found else imported


# ------------------------------------------------------------------ paramètres de connexion

def _validate_client(data: dict) -> dict:
    if "installed" not in data:
        kind = next(iter(data), "inconnu")
        raise ValueError(
            f"Ce fichier est un identifiant de type « {kind} ». FaceTag a besoin d'un ID client OAuth "
            "de type « Application de bureau » (Desktop app).")
    inst = data["installed"]
    if not inst.get("client_id") or not inst.get("client_secret"):
        raise ValueError("Le fichier ne contient pas client_id / client_secret.")
    return data


def import_client_file(src: str) -> dict:
    """Copie le JSON téléchargé depuis Google Cloud Console dans le dossier de données."""
    try:
        data = json.loads(Path(src).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"Fichier illisible : {exc}") from exc
    _validate_client(data)
    client_secret_path().write_text(json.dumps(data, indent=2), encoding="utf-8")
    token_path().unlink(missing_ok=True)  # nouvel identifiant : nouvelle connexion
    return client_info()


def save_client_ids(client_id: str, client_secret: str) -> dict:
    """Variante sans fichier : ID client et code secret copiés depuis la console Google."""
    client_id, client_secret = client_id.strip(), client_secret.strip()
    if not client_id.endswith(".apps.googleusercontent.com") or not client_secret:
        raise ValueError("ID client (…apps.googleusercontent.com) et code secret requis.")
    data = {"installed": {
        "client_id": client_id, "client_secret": client_secret,
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": ["http://localhost"],
    }}
    client_secret_path().write_text(json.dumps(data, indent=2), encoding="utf-8")
    token_path().unlink(missing_ok=True)
    return client_info()


def client_info() -> dict | None:
    """Résumé de l'identifiant configuré (sans le secret), ou None."""
    p = find_client_secret()
    if not p.exists():
        return None
    try:
        inst = json.loads(p.read_text(encoding="utf-8"))["installed"]
    except (OSError, ValueError, KeyError):
        return None
    cid = inst.get("client_id", "")
    return {"project": inst.get("project_id", "—"), "client_id": cid[:12] + "…" + cid[-30:] if cid else "",
            "path": str(p), "bundled": p == BUNDLED_CLIENT}


def connection_status() -> dict:
    """État local de la connexion (aucun appel réseau)."""
    status = {"configured": client_info() is not None, "connected": False, "read": False, "write": False}
    tok = token_path()
    if tok.exists():
        try:
            scopes = set(json.loads(tok.read_text(encoding="utf-8")).get("scopes") or [])
        except (OSError, ValueError):
            scopes = set()
        status.update(connected=bool(scopes), read=SCOPE_READ in scopes or SCOPE_WRITE in scopes,
                      write=SCOPE_WRITE in scopes)
    return status


def sign_in(write: bool = False) -> dict:
    """Ouvre le navigateur pour autoriser FaceTag (lecture seule, ou lecture + écriture)."""
    get_credentials((SCOPE_READ, SCOPE_WRITE) if write else (SCOPE_READ,))
    return connection_status()


def test_connection() -> int:
    """Vérifie l'accès en lisant la liste des contacts. Renvoie le nombre de contacts."""
    service = build("people", "v1", credentials=get_credentials(), cache_discovery=False)
    resp = service.people().connections().list(
        resourceName="people/me", pageSize=1, personFields="names").execute()
    return int(resp.get("totalItems", resp.get("totalPeople", 0)))


def sign_out(revoke: bool = True) -> None:
    """Supprime le jeton local et, si possible, révoque l'autorisation chez Google."""
    tok = token_path()
    if revoke and tok.exists():
        try:
            data = json.loads(tok.read_text(encoding="utf-8"))
            token = data.get("refresh_token") or data.get("token")
            if token:
                requests.post(REVOKE_URL, params={"token": token}, timeout=15,
                              headers={"Content-type": "application/x-www-form-urlencoded"})
        except (OSError, ValueError, requests.RequestException):
            pass  # la suppression locale suffit à déconnecter FaceTag
    tok.unlink(missing_ok=True)


def remove_client() -> None:
    sign_out()
    client_secret_path().unlink(missing_ok=True)


class GoogleAuthError(RuntimeError):
    pass


def get_credentials(scopes=(SCOPE_READ,)) -> Credentials:
    """Jeton OAuth local. Si une autorisation supplémentaire est nécessaire (ex. écriture),
    le navigateur s'ouvre pour la demander, en conservant les autorisations déjà accordées."""
    creds = None
    tok = token_path()
    needed = set(scopes)
    if tok.exists():
        creds = Credentials.from_authorized_user_file(str(tok))
        granted = set(creds.scopes or [])
        missing = not needed <= granted
        needed |= granted  # ne jamais perdre une autorisation déjà accordée
        if missing:
            creds = None  # autorisation à compléter
    if creds and creds.valid:
        return creds
    refreshed = False
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            refreshed = True
        except RefreshError:
            # En mode « Test », Google fait expirer l'autorisation après 7 jours : on la redemande.
            print("Autorisation Google expirée : nouvelle connexion nécessaire.")
    if not refreshed:
        secret = find_client_secret()
        if not secret.exists():
            raise GoogleAuthError(
                "Aucun identifiant Google configuré.\n"
                "Ouvrez « Paramètres » dans l'application et importez le fichier JSON de votre "
                "ID client OAuth « Application de bureau » (voir README)."
            )
        flow = InstalledAppFlow.from_client_secrets_file(str(secret), sorted(needed))
        creds = flow.run_local_server(port=0)  # ouvre le navigateur pour l'autorisation
    tok.write_text(creds.to_json(), encoding="utf-8")
    return creds


def iter_contacts(service):
    """Parcourt tous les contacts, page par page."""
    page_token = None
    while True:
        resp = service.people().connections().list(
            resourceName="people/me",
            pageSize=1000,
            personFields="names,photos",
            pageToken=page_token,
        ).execute()
        yield from resp.get("connections", [])
        page_token = resp.get("nextPageToken")
        if not page_token:
            break


def primary(items: list[dict]) -> dict | None:
    """Renvoie l'élément marqué primary, sinon le premier."""
    for it in items:
        if it.get("metadata", {}).get("primary"):
            return it
    return items[0] if items else None


PHOTO_SIZE = 800  # px sur le côté le plus long


def sized_url(url: str, size: int = PHOTO_SIZE) -> str:
    """Demande une version plus grande de la photo.

    Doc Google « Image sizing » : syntaxe url=param, où sN fixe le plus grand côté à N px.
    Les URL renvoyées par l'API se terminent souvent par une petite taille (ex. =s100).
    """
    base = re.sub(r"=[a-z0-9-]+$", "", url, flags=re.IGNORECASE)
    return f"{base}=s{size}"


def download_photo(url: str, resource: str) -> str | None:
    """Télécharge la photo de profil. Retourne le chemin local, ou None si échec."""
    name = hashlib.sha1(resource.encode()).hexdigest()[:16] + ".jpg"
    dest = contact_photos_dir() / name
    try:
        r = requests.get(sized_url(url), timeout=20)
        if r.status_code >= 400:
            r = requests.get(url, timeout=20)  # repli sur l'URL d'origine
        r.raise_for_status()
    except requests.RequestException as exc:
        print(f"  ! photo non téléchargée ({resource}) : {exc}")
        return None
    dest.write_bytes(r.content)
    STATS["url_endings"][re.sub(r"[0-9]+", "N", url.rsplit("/", 1)[-1][-12:])] += 1
    STATS["sizes"].append(_image_side(r.content))
    return str(dest)


STATS = {"url_endings": Counter(), "sizes": []}


def _image_side(data: bytes) -> int:
    img = cv.imdecode(np.frombuffer(data, np.uint8), cv.IMREAD_UNCHANGED)
    return 0 if img is None else max(img.shape[:2])


def sync() -> None:
    service = build("people", "v1", credentials=get_credentials(), cache_discovery=False)
    conn = db.connect()
    n_total = n_named = n_photo = 0

    for person in iter_contacts(service):
        n_total += 1
        name_obj = primary(person.get("names", []))
        if not name_obj or not name_obj.get("displayName"):
            continue  # contact sans nom : inutile pour le marquage
        n_named += 1
        resource = person["resourceName"]

        # Les contacts sans vraie photo ont un avatar générique marqué "default": true.
        photos = [p for p in person.get("photos", []) if not p.get("default")]
        photo_path = None
        if photos:
            photo_path = download_photo(primary(photos)["url"], resource)
            if photo_path:
                n_photo += 1

        db.upsert_person(conn, name_obj["displayName"], resource, photo_path)

    conn.commit()
    conn.close()
    print(f"Contacts lus : {n_total} | avec nom : {n_named} | avec photo de profil : {n_photo}")
    sizes = [s for s in STATS["sizes"] if s]
    if sizes:
        sizes.sort()
        print(f"Taille des photos téléchargées (côté max) : min {sizes[0]} | "
              f"médiane {sizes[len(sizes) // 2]} | max {sizes[-1]} px")
        print("Fin d'URL la plus fréquente (diagnostic, chiffres masqués) : "
              f"{STATS['url_endings'].most_common(3)}")


def update_contact_photo(resource: str, jpeg: bytes, person_id: int | None = None) -> None:
    """Remplace la photo du contact Google (People API people.updateContactPhoto, photoBytes en base64)."""
    import base64
    creds = get_credentials((SCOPE_READ, SCOPE_WRITE))
    service = build("people", "v1", credentials=creds, cache_discovery=False)
    service.people().updateContactPhoto(
        resourceName=resource, body={"photoBytes": base64.b64encode(jpeg).decode("ascii")}
    ).execute()
    if person_id is not None:  # copie locale, comme pour les photos importées
        dest = contact_photos_dir() / (hashlib.sha1(resource.encode()).hexdigest()[:16] + ".jpg")
        dest.write_bytes(jpeg)
        conn = db.connect()
        conn.execute("UPDATE persons SET contact_photo = ? WHERE id = ?", (str(dest), person_id))
        conn.commit()
        conn.close()


def create_contact(given: str, family: str, person_id: int | None = None) -> tuple[str, str]:
    """Crée un contact Google (prénom + nom). Si person_id est fourni, relie la personne FaceTag
    à ce nouveau contact. Renvoie (resourceName, nom affiché par Google)."""
    given, family = given.strip(), family.strip()
    if not (given or family):
        raise ValueError("Prénom ou nom requis.")
    creds = get_credentials((SCOPE_READ, SCOPE_WRITE))
    service = build("people", "v1", credentials=creds, cache_discovery=False)
    name = {k: v for k, v in (("givenName", given), ("familyName", family)) if v}
    person = service.people().createContact(body={"names": [name]}, personFields="names").execute()
    resource = person["resourceName"]
    display = (person.get("names") or [{}])[0].get("displayName") or " ".join([given, family]).strip()
    if person_id is not None:
        conn = db.connect()
        old = conn.execute("SELECT name FROM persons WHERE id = ?", (person_id,)).fetchone()
        conn.execute("UPDATE persons SET google_resource = ?, name = ?, updated_at = datetime('now') "
                     "WHERE id = ?", (resource, display, person_id))
        if old and old["name"] != display:
            db.mark_person_photos(conn, person_id)
        conn.commit()
        conn.close()
    return resource, display


if __name__ == "__main__":
    try:
        sync()
    except GoogleAuthError as exc:
        sys.exit(str(exc))
