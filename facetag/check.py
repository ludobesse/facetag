"""Diagnostic de la détection : compare plusieurs réglages sur un échantillon.

Ne modifie pas la base. Écrit des images annotées dans le sous-dossier debug\\ du projet
pour un contrôle visuel.

Exemples :
    python -m facetag.check photos --n 60          # échantillon de photos déjà scannées
    python -m facetag.check contacts               # photos de contacts sans visage détecté
"""

import argparse
import random
import time
from pathlib import Path

import cv2 as cv
import numpy as np

from . import db, models
from .engine import portrait_variant

PROJECT_DIR = Path(__file__).resolve().parent.parent
DEBUG_DIR = PROJECT_DIR / "debug"

# (nom, seuil de score, côté max en px ; None = taille d'origine)
CONFIGS = [
    ("actuel 0.8/1280", 0.8, 1280),
    ("0.7/1280", 0.7, 1280),
    ("0.6/1280", 0.6, 1280),
    ("0.7/1920", 0.7, 1920),
    ("0.7/origine", 0.7, None),
]


CONTACT_CONFIGS = [
    ("tel quel 0.6", 0.6, None, None),
    ("x320 sans marge", 0.6, None, (320, 0.0)),
    ("x320 marge 40%", 0.6, None, (320, 0.4)),
    ("x480 marge 60%", 0.6, None, (480, 0.6)),
    ("x480 marge 60% 0.5", 0.5, None, (480, 0.6)),
]


def detect(detector, img, max_side):
    h0, w0 = img.shape[:2]
    scale = 1.0 if max_side is None else min(1.0, max_side / max(h0, w0))
    small = cv.resize(img, (round(w0 * scale), round(h0 * scale)),
                      interpolation=cv.INTER_AREA) if scale < 1 else img
    detector.setInputSize((small.shape[1], small.shape[0]))
    _, rows = detector.detect(small)
    if rows is None:
        return []
    # coordonnées ramenées à l'image d'origine, + taille du visage dans l'image analysée
    return [(r[:4] / scale, float(r[14]), float(min(r[2], r[3]))) for r in rows]


def annotate(img, results, path):
    out = img.copy()
    thick = max(2, round(max(img.shape[:2]) / 600))
    for box, score, px in results:
        x, y, w, h = map(int, box)
        color = (0, 200, 0) if px >= 24 else (0, 165, 255)  # orange = trop petit (< 24 px), ignoré
        cv.rectangle(out, (x, y), (x + w, y + h), color, thick)
        cv.putText(out, f"{score:.2f}", (x, max(0, y - 6)), cv.FONT_HERSHEY_SIMPLEX,
                   thick * 0.4, color, thick)
    scale = min(1.0, 1600 / max(out.shape[:2]))
    if scale < 1:
        out = cv.resize(out, None, fx=scale, fy=scale, interpolation=cv.INTER_AREA)
    cv.imwrite(str(path), out, [cv.IMWRITE_JPEG_QUALITY, 85])


def load(path):
    try:
        data = Path(path).read_bytes()
    except OSError:
        return None
    return cv.imdecode(np.frombuffer(data, np.uint8), cv.IMREAD_COLOR)


def run(paths, label, min_px, configs=None):
    configs = configs or [(n, t, m, None) for n, t, m in CONFIGS]
    best_name = configs[2][0]
    det_path = str(models.detector_path())
    detectors = {name: cv.FaceDetectorYN.create(det_path, "", (320, 320), thr, 0.3, 5000)
                 for name, thr, _, _ in configs}
    out_dir = DEBUG_DIR / label
    out_dir.mkdir(parents=True, exist_ok=True)
    for f in out_dir.glob("*.jpg"):
        f.unlink()

    totals = {name: [0, 0.0] for name, _, _, _ in configs}  # visages exploitables, temps
    sizes = []
    n = 0
    for i, p in enumerate(paths):
        img = load(p)
        if img is None:
            continue
        n += 1
        sizes.append(max(img.shape[:2]))
        best = None
        best_img = img
        for name, _, max_side, variant in configs:
            src = portrait_variant(img, *variant) if variant else img
            t = time.perf_counter()
            res = detect(detectors[name], src, max_side)
            totals[name][1] += time.perf_counter() - t
            totals[name][0] += sum(1 for _, _, px in res if px >= min_px)
            if name == best_name:
                best, best_img = res, src
        if best:
            annotate(best_img, best, out_dir / f"{i:03d}.jpg")

    print(f"\n{label} : {n} images analysées | côté max médian : {int(np.median(sizes)) if sizes else 0} px")
    print(f"{'Réglage':<20}{'Visages':>9}{'Par image':>11}{'ms/image':>10}")
    for name, _, _, _ in configs:
        faces, t = totals[name]
        print(f"{name:<20}{faces:>9}{faces / max(n, 1):>11.2f}{t / max(n, 1) * 1000:>10.0f}")
    print(f"\nImages annotées (réglage « {best_name} », vert = retenu, orange = trop petit) : {out_dir}")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Diagnostic de détection FaceTag")
    ap.add_argument("what", choices=["photos", "contacts"])
    ap.add_argument("--n", type=int, default=60, help="taille de l'échantillon (photos)")
    a = ap.parse_args(argv)
    conn = db.connect()

    if a.what == "photos":
        rows = [r["path"] for r in conn.execute(
            "SELECT path FROM photos WHERE scanned_at IS NOT NULL AND error IS NULL")]
        random.seed(42)
        run(random.sample(rows, min(a.n, len(rows))), "photos", min_px=24)
    else:
        rows = [r["contact_photo"] for r in conn.execute(
            "SELECT p.contact_photo FROM persons p WHERE p.contact_photo IS NOT NULL "
            "AND NOT EXISTS (SELECT 1 FROM faces f WHERE f.person_id = p.id AND f.status = 'reference')")]
        run(rows, "contacts", min_px=16, configs=CONTACT_CONFIGS)
        mosaic(rows, DEBUG_DIR / "contacts_sans_visage.jpg")


def mosaic(paths, dest, tile=96, cols=20):
    """Planche-contact des photos de contacts sans visage détecté, pour un contrôle visuel rapide."""
    tiles = []
    for p in paths:
        img = load(p)
        if img is None:
            continue
        h, w = img.shape[:2]
        s = tile / max(h, w)
        img = cv.resize(img, (max(1, round(w * s)), max(1, round(h * s))), interpolation=cv.INTER_AREA)
        canvas = np.full((tile, tile, 3), 255, np.uint8)
        y, x = (tile - img.shape[0]) // 2, (tile - img.shape[1]) // 2
        canvas[y:y + img.shape[0], x:x + img.shape[1]] = img
        tiles.append(canvas)
    if not tiles:
        return
    while len(tiles) % cols:
        tiles.append(np.full((tile, tile, 3), 255, np.uint8))
    rows = [np.hstack(tiles[i:i + cols]) for i in range(0, len(tiles), cols)]
    dest.parent.mkdir(parents=True, exist_ok=True)
    cv.imwrite(str(dest), np.vstack(rows), [cv.IMWRITE_JPEG_QUALITY, 85])
    print(f"Planche-contact ({len(paths)} photos sans visage) : {dest}")


if __name__ == "__main__":
    main()
