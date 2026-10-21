"""Installation automatique d'ExifTool pour Windows (téléchargement officiel + vérification).

Sources officielles (page d'accueil https://exiftool.org/) :
- version actuelle  : https://exiftool.org/ver.txt
- empreintes        : https://exiftool.org/checksums-<version>.txt   (lignes « SHA2-256(fichier)= … »)
- archive Windows   : https://sourceforge.net/projects/exiftool/files/exiftool-<version>_64.zip/download

L'archive contient « exiftool(-k).exe » et le dossier « exiftool_files » ; d'après la page
d'installation officielle, il faut renommer l'exécutable en « exiftool.exe » et le garder
avec « exiftool_files ». L'installation se fait dans %LOCALAPPDATA%\\FaceTag\\exiftool
(pas de droits administrateur nécessaires).
"""

import hashlib
import os
import re
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

import requests

from . import config
from .paths import data_dir

VER_URL = "https://exiftool.org/ver.txt"
SUMS_URL = "https://exiftool.org/checksums-{v}.txt"
ZIP_NAME = "exiftool-{v}_{bits}.zip"
ZIP_URL = "https://sourceforge.net/projects/exiftool/files/{name}/download"
UA = {"User-Agent": "FaceTag (installation ExifTool)"}


class InstallError(RuntimeError):
    pass


def install_dir() -> Path:
    return data_dir() / "exiftool"


def supported() -> bool:
    return sys.platform == "win32"


def manual_help() -> str:
    if sys.platform == "darwin":
        return "Sur macOS : installez le paquet ExifTool depuis https://exiftool.org/ (fichier .pkg)."
    if sys.platform.startswith("linux"):
        return ("Sous Linux : installez le paquet de votre distribution, par exemple "
                "« sudo apt install libimage-exiftool-perl ».")
    return "Téléchargez ExifTool sur https://exiftool.org/."


def _expected_sha256(version: str, name: str) -> str:
    r = requests.get(SUMS_URL.format(v=version), headers=UA, timeout=30)
    r.raise_for_status()
    m = re.search(r"SHA2-256\(" + re.escape(name) + r"\)=\s*([0-9a-fA-F]{64})", r.text)
    if not m:
        raise InstallError(f"Empreinte de {name} introuvable dans le fichier officiel.")
    return m.group(1).lower()


def install(progress=None, log=print) -> str:
    """Télécharge, vérifie et installe ExifTool. Renvoie le chemin de exiftool.exe."""
    if not supported():
        raise InstallError(manual_help())
    try:
        version = requests.get(VER_URL, headers=UA, timeout=30).text.strip()
        if not re.fullmatch(r"\d+\.\d+", version):
            raise InstallError(f"Réponse inattendue de {VER_URL} : {version[:40]!r}")
        bits = "64" if sys.maxsize > 2 ** 32 else "32"
        name = ZIP_NAME.format(v=version, bits=bits)
        log(f"Version {version} : vérification de l'empreinte officielle…")
        expected = _expected_sha256(version, name)

        log(f"Téléchargement de {name}…")
        tmp = Path(tempfile.mkdtemp(prefix="facetag_exiftool_"))
        archive = tmp / name
        h = hashlib.sha256()
        with requests.get(ZIP_URL.format(name=name), headers=UA, timeout=60, stream=True) as r:
            r.raise_for_status()
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            with archive.open("wb") as f:
                for chunk in r.iter_content(1 << 16):
                    f.write(chunk)
                    h.update(chunk)
                    done += len(chunk)
                    if progress:
                        progress(done, total)
    except requests.RequestException as exc:
        raise InstallError(f"Téléchargement impossible : {exc}") from exc

    if h.hexdigest() != expected:
        shutil.rmtree(tmp, ignore_errors=True)
        raise InstallError("Empreinte SHA-256 incorrecte : fichier rejeté (rien n'a été installé).")
    log("Empreinte vérifiée. Installation…")

    try:
        with zipfile.ZipFile(archive) as z:
            z.extractall(tmp / "x")
    except zipfile.BadZipFile as exc:
        shutil.rmtree(tmp, ignore_errors=True)
        raise InstallError(f"Archive invalide : {exc}") from exc
    exe = next((p for p in (tmp / "x").rglob("exiftool*.exe")), None)
    if exe is None:
        shutil.rmtree(tmp, ignore_errors=True)
        raise InstallError("exiftool(-k).exe introuvable dans l'archive.")

    dest = install_dir()
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(exe.parent, dest)
    final = dest / "exiftool.exe"
    os.replace(dest / exe.name, final)       # « exiftool(-k).exe » -> « exiftool.exe »
    shutil.rmtree(tmp, ignore_errors=True)
    config.set("exiftool_path", str(final))
    log(f"ExifTool {version} installé dans {dest}")
    return str(final)
