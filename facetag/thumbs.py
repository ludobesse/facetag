"""Vignettes de visages, en cache local (%LOCALAPPDATA%\\FaceTag\\thumbs).

Créées pendant le scan (l'image est déjà en mémoire), sinon à la demande par l'application.
"""

from pathlib import Path

import cv2 as cv
import numpy as np

from .paths import data_dir

SIZE = 160
MARGIN = 0.35  # marge autour du visage, en fraction de sa taille


def thumbs_dir() -> Path:
    d = data_dir() / "thumbs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def path(face_id: int) -> Path:
    return thumbs_dir() / f"{face_id // 1000:04d}" / f"{face_id}.jpg"


def crop(img: np.ndarray, x: float, y: float, w: float, h: float) -> np.ndarray:
    H, W = img.shape[:2]
    cx, cy = (x + w / 2) * W, (y + h / 2) * H
    side = max(w * W, h * H) * (1 + 2 * MARGIN)
    x0, y0 = int(max(0, cx - side / 2)), int(max(0, cy - side / 2))
    x1, y1 = int(min(W, cx + side / 2)), int(min(H, cy + side / 2))
    c = img[y0:y1, x0:x1]
    if c.size == 0:
        c = img
    s = SIZE / max(c.shape[:2])
    return cv.resize(c, (max(1, round(c.shape[1] * s)), max(1, round(c.shape[0] * s))),
                     interpolation=cv.INTER_AREA if s < 1 else cv.INTER_CUBIC)


def portrait(img: np.ndarray, x, y, w, h, margin: float = 0.6, size: int = 720) -> np.ndarray:
    """Recadrage carré centré sur le visage (pour une photo de contact), décalé si besoin
    pour rester dans l'image, puis réduit à `size` px maximum."""
    H, W = img.shape[:2]
    cx, cy = (x + w / 2) * W, (y + h / 2) * H
    side = min(max(w * W, h * H) * (1 + 2 * margin), W, H)
    x0 = int(round(min(max(cx - side / 2, 0), W - side)))
    y0 = int(round(min(max(cy - side / 2, 0), H - side)))
    s = int(round(side))
    c = img[y0:y0 + s, x0:x0 + s]
    if max(c.shape[:2]) > size:
        c = cv.resize(c, (size, size), interpolation=cv.INTER_AREA)
    return c


def portrait_jpeg(photo_path: str, x, y, w, h) -> bytes | None:
    try:
        data = Path(photo_path).read_bytes()
    except OSError:
        return None
    img = cv.imdecode(np.frombuffer(data, np.uint8), cv.IMREAD_COLOR)  # orientation EXIF appliquée
    if img is None:
        return None
    ok, buf = cv.imencode(".jpg", portrait(img, x, y, w, h), [cv.IMWRITE_JPEG_QUALITY, 90])
    return buf.tobytes() if ok else None


def save(face_id: int, img: np.ndarray, x, y, w, h) -> None:
    p = path(face_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    ok, buf = cv.imencode(".jpg", crop(img, x, y, w, h), [cv.IMWRITE_JPEG_QUALITY, 85])
    if ok:
        p.write_bytes(buf.tobytes())


def ensure(face_id: int, photo_path: str, x, y, w, h) -> Path | None:
    """Renvoie la vignette, en la créant depuis la photo d'origine si besoin."""
    p = path(face_id)
    if p.exists():
        return p
    try:
        data = Path(photo_path).read_bytes()
    except OSError:
        return None
    img = cv.imdecode(np.frombuffer(data, np.uint8), cv.IMREAD_COLOR)  # orientation EXIF appliquée
    if img is None:
        return None
    save(face_id, img, x, y, w, h)
    return p if p.exists() else None
