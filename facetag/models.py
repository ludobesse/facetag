"""Téléchargement et vérification des modèles OpenCV Zoo (YuNet + SFace).

Fichiers, tailles et empreintes SHA-256 relevés dans le dépôt officiel
https://github.com/opencv/opencv_zoo (commit du 28.05.2026) :
- YuNet  : licence MIT
- SFace  : licence Apache 2.0

Choix de la version YuNet (README OpenCV Zoo) :
- OpenCV 5.x -> face_detection_yunet_2026may.onnx (entrée dynamique, requise par le moteur ONNX Runtime de 5.x)
- OpenCV 4.x -> face_detection_yunet_2023mar.onnx (entrée fixe, compatible DNN 4.x)
"""

import hashlib
import sys
from pathlib import Path

import cv2 as cv
import requests

from .paths import data_dir

BASE = "https://github.com/opencv/opencv_zoo/raw/main/models"

MODELS = {
    "yunet_2026may": (
        f"{BASE}/face_detection_yunet/face_detection_yunet_2026may.onnx",
        "ebafce4e3c118d6554634be5c27ab333b4c047a9a8c3faf1d7cf93101c22f0f0",
    ),
    "yunet_2023mar": (
        f"{BASE}/face_detection_yunet/face_detection_yunet_2023mar.onnx",
        "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4",
    ),
    "sface": (
        f"{BASE}/face_recognition_sface/face_recognition_sface_2021dec.onnx",
        "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79",
    ),
}


def opencv_major() -> int:
    return int(cv.__version__.split(".")[0])


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def ensure(key: str) -> Path:
    """Renvoie le chemin local du modèle, en le téléchargeant si besoin."""
    url, expected = MODELS[key]
    dest = data_dir() / "models" / url.rsplit("/", 1)[1]
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and _sha256(dest) == expected:
        return dest

    print(f"Téléchargement du modèle {dest.name}…")
    tmp = dest.with_suffix(".part")
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        with tmp.open("wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
    got = _sha256(tmp)
    if got != expected:
        tmp.unlink(missing_ok=True)
        sys.exit(f"Empreinte SHA-256 incorrecte pour {dest.name} (reçu {got}). Téléchargement annulé.")
    tmp.replace(dest)
    return dest


def detector_path() -> Path:
    return ensure("yunet_2026may" if opencv_major() >= 5 else "yunet_2023mar")


def recognizer_path() -> Path:
    return ensure("sface")
