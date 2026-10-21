"""Moteur visages : détection YuNet + empreintes SFace (OpenCV).

Indépendant de toute interface et de la base : prend une image, rend des visages.
Pourra être déplacé tel quel dans un conteneur (ex. sur le NAS) plus tard.
"""

from dataclasses import dataclass

import cv2 as cv
import numpy as np

from . import models

# À incrémenter quand un réglage de détection change : les photos analysées avec une version
# antérieure sont réanalysées au prochain scan (visages confirmés/ignorés conservés).
DETECT_VERSION = 2

# Seuil de similarité cosinus recommandé par OpenCV Zoo pour SFace (sface.py : 0.363).
SFACE_COSINE_THRESHOLD = 0.363

# YuNet est entraîné pour des visages d'environ 10x10 à 300x300 px (README OpenCV Zoo).
# On réduit donc les grandes photos : côté long = DETECT_MAX_SIDE.
# 1280 px : un portrait serré reste < 300 px de visage, un petit visage de groupe reste > 10 px
# dans la plupart des cas. À ajuster après le banc d'essai.
DETECT_MAX_SIDE = 1280


@dataclass
class Face:
    x: float          # zone normalisée 0..1, coin haut-gauche (converti en centre MWG à l'écriture)
    y: float
    w: float
    h: float
    score: float
    embedding: np.ndarray  # float32, normalisé L2 (longueur 1)


class FaceEngine:
    # 0.6 retenu après banc d'essai (60 photos) : 13 visages contre 9 à 0.8, dont 1 faux positif.
    # Les faux positifs pourront être ignorés dans l'application.
    def __init__(self, score_threshold: float = 0.6):
        self.detector = cv.FaceDetectorYN.create(
            str(models.detector_path()), "", (320, 320), score_threshold, 0.3, 5000
        )
        self.recognizer = cv.FaceRecognizerSF.create(str(models.recognizer_path()), "")

    @staticmethod
    def decode(data: bytes) -> np.ndarray | None:
        """Décode un JPEG en mémoire. OpenCV applique l'orientation EXIF par défaut."""
        arr = np.frombuffer(data, dtype=np.uint8)
        return cv.imdecode(arr, cv.IMREAD_COLOR)

    def analyze(self, img: np.ndarray, min_face_px: int = 24) -> list[Face]:
        h0, w0 = img.shape[:2]
        scale = min(1.0, DETECT_MAX_SIDE / max(h0, w0))
        small = cv.resize(img, (round(w0 * scale), round(h0 * scale)),
                          interpolation=cv.INTER_AREA) if scale < 1 else img
        hs, ws = small.shape[:2]

        self.detector.setInputSize((ws, hs))
        _, rows = self.detector.detect(small)
        if rows is None:
            return []

        faces = []
        for row in rows:
            x, y, w, h = row[:4]
            if min(w, h) < min_face_px:
                continue  # trop petit pour une reconnaissance fiable
            aligned = self.recognizer.alignCrop(small, row)
            feat = self.recognizer.feature(aligned).flatten().astype(np.float32)
            feat /= np.linalg.norm(feat) or 1.0
            faces.append(Face(
                # float() indispensable : un float32 NumPy serait stocké en octets par SQLite
                x=max(0.0, float(x) / ws), y=max(0.0, float(y) / hs),
                w=min(float(w) / ws, 1.0), h=min(float(h) / hs, 1.0),
                score=float(row[14]), embedding=feat,
            ))
        return faces

    def largest_face(self, img: np.ndarray) -> Face | None:
        """Pour les photos de contacts : on garde le visage principal.

        Les photos de profil sont souvent minuscules (~96 px) et cadrées serré sur le visage.
        Si rien n'est trouvé tel quel, on essaie avec une marge autour et un agrandissement.
        """
        for variant in (img, portrait_variant(img, 320, 0.4), portrait_variant(img, 480, 0.6)):
            faces = self.analyze(variant, min_face_px=16)
            if faces:
                return max(faces, key=lambda f: f.w * f.h)
        return None


def portrait_variant(img: np.ndarray, target: int, pad: float) -> np.ndarray:
    """Ajoute une marge (pad × côté, de chaque côté) puis agrandit à `target` px (côté max)."""
    h, w = img.shape[:2]
    p = round(max(h, w) * pad)
    out = cv.copyMakeBorder(img, p, p, p, p, cv.BORDER_REPLICATE)
    scale = target / max(out.shape[:2])
    if scale > 1:
        out = cv.resize(out, None, fx=scale, fy=scale, interpolation=cv.INTER_CUBIC)
    return out
