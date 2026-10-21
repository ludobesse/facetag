"""Étape 5 : écriture des noms confirmés dans les photos (XMP, norme MWG) via ExifTool.

Ce qui est écrit dans chaque photo concernée :
- XMP-mwg-rs:RegionInfo : une zone « Face » par visage confirmé, avec le nom.
  Norme MWG 2.0 : « Region metadata is applied to the stored image » -> les zones sont converties
  depuis l'image affichée (orientation EXIF appliquée par OpenCV) vers l'image stockée.
  Area X/Y = CENTRE de la zone, W/H = largeur/hauteur, valeurs normalisées 0..1.
- XMP-iptcExt:PersonInImage : la liste des noms (champ IPTC standard, utile pour la recherche).

Zones déjà présentes (ex. écrites par digiKam) : conservées, SAUF celles qui recouvrent un
visage connu de FaceTag (elles sont remplacées par la version FaceTag).

Par défaut : SIMULATION, rien n'est modifié. Ajouter --apply pour écrire.
Usage :
    python -m facetag.write                          # simulation
    python -m facetag.write --apply --folder "\\\\NAS\\photo\\Test"   # écrire sur un dossier test
    python -m facetag.write --apply                  # tout écrire
"""

import argparse
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from . import db

PROJECT_DIR = Path(__file__).resolve().parent.parent
CHUNK = 100
OVERLAP_IOU = 0.3


# ---------------------------------------------------------------- ExifTool

def find_exiftool() -> str | None:
    from . import config
    custom = config.get("exiftool_path")
    if custom and Path(custom).exists():
        return custom
    for p in (PROJECT_DIR / "tools" / "exiftool" / "exiftool.exe",
              PROJECT_DIR / "tools" / "exiftool.exe"):
        if p.exists():
            return str(p)
    return shutil.which("exiftool")


def exiftool_version(exe: str | None = None) -> str:
    """Version d'ExifTool (pour le bouton « Tester » des Paramètres)."""
    exe = exe or find_exiftool()
    if not exe:
        raise RuntimeError("ExifTool introuvable.")
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    r = subprocess.run([exe, "-ver"], capture_output=True, text=True, timeout=30, creationflags=flags)
    if r.returncode != 0 or not r.stdout.strip():
        raise RuntimeError(r.stderr.strip() or "ExifTool ne répond pas.")
    return r.stdout.strip()


def _run(exe: str, args: list[str], files: list[str]) -> subprocess.CompletedProcess:
    """Lance ExifTool avec la liste de fichiers dans un fichier d'arguments UTF-8
    (recommandé par la doc ExifTool pour les noms de fichiers Unicode sous Windows)."""
    with tempfile.NamedTemporaryFile("w", suffix=".args", delete=False, encoding="utf-8") as f:
        f.write("\n".join(files) + "\n")
        argfile = f.name
    try:
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        return subprocess.run([exe, "-charset", "filename=utf8", *args, "-@", argfile],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", creationflags=flags)
    finally:
        os.unlink(argfile)


def read_meta(exe: str, files: list[str]) -> dict[str, dict]:
    r = _run(exe, ["-j", "-struct", "-n", "-G1", "-XMP-mwg-rs:RegionInfo", "-IFD0:Orientation",
                   "-File:ImageWidth", "-File:ImageHeight"], files)
    try:
        data = json.loads(r.stdout or "[]")
    except json.JSONDecodeError:
        data = []
    return {os.path.normcase(os.path.normpath(d["SourceFile"])): d for d in data}


# ------------------------------------------------------- géométrie / orientation

def displayed_to_stored(px: float, py: float, orientation: int) -> tuple[float, float]:
    """Point normalisé de l'image affichée -> image stockée, pour Orientation EXIF 1..8.
    Formules vérifiées par test (image annotée + ExifTool + décodage OpenCV)."""
    x, y = px, py
    return {
        1: (x, y), 2: (1 - x, y), 3: (1 - x, 1 - y), 4: (x, 1 - y),
        5: (y, x), 6: (y, 1 - x), 7: (1 - y, 1 - x), 8: (1 - y, x),
    }.get(orientation, (x, y))


def box_to_stored(x, y, w, h, orientation) -> tuple[float, float, float, float]:
    """Boîte (coin haut-gauche, affichée) -> boîte (coin haut-gauche, stockée)."""
    pts = [displayed_to_stored(px, py, orientation)
           for px, py in ((x, y), (x + w, y), (x, y + h), (x + w, y + h))]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)


def iou(a, b) -> float:
    ax2, ay2, bx2, by2 = a[0] + a[2], a[1] + a[3], b[0] + b[2], b[1] + b[3]
    iw = max(0.0, min(ax2, bx2) - max(a[0], b[0]))
    ih = max(0.0, min(ay2, by2) - max(a[1], b[1]))
    inter = iw * ih
    union = a[2] * a[3] + b[2] * b[3] - inter
    return inter / union if union else 0.0


def _as_list(v):
    return v if isinstance(v, list) else ([] if v is None else [v])


# ---------------------------------------------------------------- construction

def build_entry(path: str, meta: dict, faces: list) -> tuple[dict, dict]:
    """Construit l'objet JSON ExifTool pour une photo. Renvoie (entrée, stats)."""
    orientation = int(meta.get("IFD0:Orientation") or 1)
    W = meta.get("File:ImageWidth")
    H = meta.get("File:ImageHeight")

    all_boxes = [box_to_stored(f["x"], f["y"], f["w"], f["h"], orientation) for f in faces]
    ours = []
    for f, b in zip(faces, all_boxes):
        if f["status"] == "confirmed" and f["name"]:
            ours.append({"Area": {"X": round(b[0] + b[2] / 2, 6), "Y": round(b[1] + b[3] / 2, 6),
                                  "W": round(b[2], 6), "H": round(b[3], 6), "Unit": "normalized"},
                         "Name": f["name"], "Type": "Face"})

    info = meta.get("XMP-mwg-rs:RegionInfo") or {}
    kept, removed = [], 0
    for reg in _as_list(info.get("RegionList")):
        a = reg.get("Area") or {}
        try:
            box = (float(a["X"]) - float(a["W"]) / 2, float(a["Y"]) - float(a["H"]) / 2,
                   float(a["W"]), float(a["H"]))
        except (KeyError, TypeError, ValueError):
            kept.append(reg)
            continue
        if reg.get("Type", "Face") == "Face" and any(iou(box, b) > OVERLAP_IOU for b in all_boxes):
            removed += 1  # remplacée par la version FaceTag
            continue
        kept.append(reg)

    if not ours and removed == 0:
        return None, {"added": 0, "replaced": 0, "kept": len(kept)}  # rien à changer dans ce fichier

    regions = kept + ours
    names = sorted({r["Name"] for r in regions if r.get("Type", "Face") == "Face" and r.get("Name")})
    entry = {"SourceFile": path, "XMP-iptcExt:PersonInImage": names or "-"}
    if regions:
        dims = {"W": W, "H": H, "Unit": "pixel"} if W and H else info.get("AppliedToDimensions")
        entry["XMP-mwg-rs:RegionInfo"] = {"RegionList": regions, **({"AppliedToDimensions": dims} if dims else {})}
    else:
        entry["XMP-mwg-rs:RegionInfo"] = "-"  # plus aucune zone : on supprime la structure
    return entry, {"added": len(ours), "replaced": removed, "kept": len(kept)}


# ---------------------------------------------------------------- exécution

def pending(conn, folder: str | None, limit: int | None):
    sql = "SELECT id, path FROM photos WHERE needs_write = 1"
    args = []
    if folder:
        sql += " AND path LIKE ? ESCAPE '\\'"
        prefix = os.path.normpath(folder).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        args.append(prefix + "%")
    sql += " ORDER BY path"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return conn.execute(sql, args).fetchall()


def run(apply: bool, folder: str | None = None, limit: int | None = None,
        backup: bool = False, progress=None, log=print) -> dict:
    exe = find_exiftool()
    if not exe:
        raise RuntimeError(
            "ExifTool introuvable. Téléchargez exiftool-XX_64.zip sur https://exiftool.org/, "
            "renommez « exiftool(-k).exe » en « exiftool.exe » et placez-le avec le dossier "
            f"« exiftool_files » dans {PROJECT_DIR / 'tools' / 'exiftool'}, ou indiquez son chemin "
            "dans les Paramètres (voir README).")

    conn = db.connect()
    photos = pending(conn, folder, limit)
    totals = {"photos": len(photos), "written": 0, "errors": 0, "added": 0, "replaced": 0, "kept": 0,
              "unchanged": 0}
    log(f"{'ÉCRITURE' if apply else 'SIMULATION'} : {len(photos)} photo(s) à mettre à jour")

    for start in range(0, len(photos), CHUNK):
        chunk = photos[start:start + CHUNK]
        paths = [p["path"] for p in chunk]
        meta = read_meta(exe, paths)
        entries, ok_ids = [], []
        for p in chunk:
            m = meta.get(os.path.normcase(os.path.normpath(p["path"])))
            if m is None:
                totals["errors"] += 1
                conn.execute("UPDATE photos SET write_error = 'lecture' WHERE id = ?", (p["id"],))
                log(f"  ! illisible par ExifTool : {p['path']}")
                continue
            faces = conn.execute(
                "SELECT f.x, f.y, f.w, f.h, f.status, pe.name FROM faces f "
                "LEFT JOIN persons pe ON pe.id = f.person_id WHERE f.photo_id = ?", (p["id"],)
            ).fetchall()
            entry, st = build_entry(p["path"], m, faces)
            for k in ("added", "replaced", "kept"):
                totals[k] += st[k]
            if entry is None:
                if apply:  # aucune modification nécessaire : fichier laissé intact
                    conn.execute("UPDATE photos SET needs_write = 0 WHERE id = ?", (p["id"],))
                totals["unchanged"] += 1
                continue
            entries.append(entry)
            ok_ids.append(p["id"])

        if apply and not entries:
            conn.commit()
        if apply and entries:
            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
                json.dump(entries, f, ensure_ascii=False)
                jfile = f.name
            try:
                args = ["-f", "-P", f"-j={jfile}"] + ([] if backup else ["-overwrite_original"])
                r = _run(exe, args, [e["SourceFile"] for e in entries])
            finally:
                os.unlink(jfile)
            errors = [line.strip() for line in r.stderr.splitlines() if line.startswith("Error")]
            for pid, e in zip(ok_ids, entries):
                src = os.path.normpath(e["SourceFile"])
                if any(line.endswith(src) or line.endswith(e["SourceFile"]) for line in errors):
                    totals["errors"] += 1
                    conn.execute("UPDATE photos SET write_error = 'écriture' WHERE id = ?", (pid,))
                    continue
                try:
                    mtime = os.stat(e["SourceFile"]).st_mtime
                except OSError:
                    mtime = None
                conn.execute("UPDATE photos SET needs_write = 0, write_error = NULL, "
                             "written_at = datetime('now'), mtime = COALESCE(?, mtime) WHERE id = ?",
                             (mtime, pid))
                totals["written"] += 1
            for line in r.stderr.splitlines():
                if line.strip():
                    log("  " + line.strip())
            conn.commit()
        if progress:
            progress(min(start + CHUNK, len(photos)), len(photos))

    conn.close()
    log(f"Zones ajoutées : {totals['added']} | remplacées : {totals['replaced']} | "
        f"autres zones conservées : {totals['kept']} | fichiers sans changement : {totals['unchanged']} | "
        f"erreurs : {totals['errors']}"
        + (f" | photos écrites : {totals['written']}" if apply else " | (simulation, rien écrit)"))
    return totals


def main(argv=None):
    ap = argparse.ArgumentParser(description="Écriture des noms dans les métadonnées (XMP MWG)")
    ap.add_argument("--apply", action="store_true", help="écrire réellement (sinon simulation)")
    ap.add_argument("--folder", help="limiter aux photos de ce dossier (pour tester)")
    ap.add_argument("--limit", type=int, help="nombre max de photos")
    ap.add_argument("--backup", action="store_true",
                    help="garder une copie *_original de chaque photo modifiée (double l'espace disque)")
    a = ap.parse_args(argv)
    try:
        run(a.apply, a.folder, a.limit, a.backup)
    except RuntimeError as exc:
        raise SystemExit(str(exc))


if __name__ == "__main__":
    main()
