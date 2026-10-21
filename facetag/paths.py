"""Emplacements des données locales de FaceTag.

Sous Windows : %LOCALAPPDATA%\\FaceTag
Ailleurs     : ~/.facetag
Rien n'est envoyé en ligne : base, photos de contacts et jeton OAuth restent ici.
"""

import os
from pathlib import Path


def data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    path = Path(base) / "FaceTag" if base else Path.home() / ".facetag"
    path.mkdir(parents=True, exist_ok=True)
    return path


def db_path() -> Path:
    return data_dir() / "facetag.db"


def token_path() -> Path:
    return data_dir() / "google_token.json"


def client_secret_path() -> Path:
    """Identifiant OAuth « Application de bureau » importé via les Paramètres."""
    return data_dir() / "google_client.json"


def contact_photos_dir() -> Path:
    path = data_dir() / "contact_photos"
    path.mkdir(parents=True, exist_ok=True)
    return path
