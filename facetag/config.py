"""Réglages de l'application (fichier JSON dans le dossier de données de l'utilisateur).

Aucun réglage n'est stocké dans le dossier du programme : on peut donc partager le code
(dépôt open source) sans y laisser d'identifiants ni de chemins personnels.
"""

import json

from .paths import data_dir


def _path():
    return data_dir() / "config.json"


def load() -> dict:
    try:
        return json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def get(key: str, default=None):
    return load().get(key, default)


def set(key: str, value) -> None:  # noqa: A001 — API simple et lisible : config.set("clé", valeur)
    data = load()
    if value is None:
        data.pop(key, None)
    else:
        data[key] = value
    _path().write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
