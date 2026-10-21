"""Étape 2 : scan des photos (sur le NAS, via le réseau) et des photos de contacts.

- Incrémental : une photo déjà analysée et non modifiée (même mtime) est ignorée.
- Reprise : la base est validée régulièrement ; Ctrl+C puis relance = reprise là où on s'était arrêté.
- Lecture réseau en parallèle (threads) pendant que le calcul tourne.

Exemples :
    python -m facetag.scan "\\\\NAS\\photo" --bench 500     # banc d'essai sur 500 photos
    python -m facetag.scan "\\\\NAS\\photo"                 # scan complet (incrémental)
    python -m facetag.scan --contacts                       # visages de référence (photos de contacts)
    python -m facetag.scan "\\\\NAS\\photo" --rescan              # tout réanalyser (après changement de réglage)
"""

import argparse
import os
import sqlite3
import sys
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import db, thumbs
from .engine import DETECT_VERSION, FaceEngine

JPEG_EXT = {".jpg", ".jpeg"}
COMMIT_EVERY = 200
SKIP_DIRS = {"@eaDir", "#recycle", "@Recycle", ".@__thumb"}  # dossiers techniques Synology/NAS


def iter_jpegs(root: str, log=print):
    """Parcours récursif rapide (os.scandir), adapté aux partages réseau."""
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    if e.is_dir(follow_symlinks=False):
                        if e.name not in SKIP_DIRS and not e.name.startswith("."):
                            stack.append(e.path)
                    elif os.path.splitext(e.name)[1].lower() in JPEG_EXT:
                        yield e.path, e.stat().st_mtime
        except OSError as exc:
            log(f"  ! dossier illisible {d} : {exc}")


def iou(a, b) -> float:
    ax2, ay2, bx2, by2 = a[0] + a[2], a[1] + a[3], b[0] + b[2], b[1] + b[3]
    iw = max(0.0, min(ax2, bx2) - max(a[0], b[0]))
    ih = max(0.0, min(ay2, by2) - max(a[1], b[1]))
    inter = iw * ih
    union = a[2] * a[3] + b[2] * b[3] - inter
    return inter / union if union else 0.0


def save_faces(conn: sqlite3.Connection, photo_id: int, faces, size, img=None) -> None:
    """Remplace les visages non confirmés ; garde les visages confirmés/ignorés par l'utilisateur."""
    kept = conn.execute(
        "SELECT x, y, w, h FROM faces WHERE photo_id = ? AND status IN ('confirmed', 'ignored')",
        (photo_id,),
    ).fetchall()
    for r in conn.execute("SELECT id FROM faces WHERE photo_id = ? AND status IN ('unknown', 'suggested')",
                          (photo_id,)).fetchall():
        thumbs.path(r["id"]).unlink(missing_ok=True)  # un id peut être réutilisé par SQLite
    conn.execute(
        "DELETE FROM faces WHERE photo_id = ? AND status IN ('unknown', 'suggested')", (photo_id,)
    )
    for f in faces:
        box = (f.x, f.y, f.w, f.h)
        if any(iou(box, tuple(k)) > 0.5 for k in kept):
            continue  # déjà traité par l'utilisateur
        cur = conn.execute(
            "INSERT INTO faces (photo_id, x, y, w, h, score, embedding) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (photo_id, *box, f.score, f.embedding.tobytes()),
        )
        if img is not None:
            thumbs.save(cur.lastrowid, img, *box)
    conn.execute(
        "UPDATE photos SET width = ?, height = ?, scanned_at = datetime('now'), error = NULL, "
        "detect_version = ? WHERE id = ?",
        (size[1], size[0], DETECT_VERSION, photo_id),
    )


def read_file(path: str, log=print) -> tuple[bytes | None, float]:
    t = time.perf_counter()
    try:
        data = Path(path).read_bytes()
    except OSError as exc:
        log(f"  ! lecture impossible {path} : {exc}")
        data = None
    return data, time.perf_counter() - t


def scan(root: str, limit: int | None = None, io_threads: int = 4, bench: bool = False,
         rescan: bool = False, log=print, progress=None, stop=None) -> dict:
    """Analyse les JPEG de `root`. `progress(fait, total)`, `stop` (threading.Event) : pour l'application."""
    root = os.path.normpath(root)  # même forme de chemin que les scans précédents (évite les doublons)
    stopped = lambda: stop is not None and stop.is_set()
    conn = db.connect()
    engine = FaceEngine()

    # 1) Liste des fichiers à traiter (nouveaux ou modifiés)
    t_list = time.perf_counter()
    known = {r["path"]: (r["id"], r["mtime"], r["scanned_at"], r["detect_version"], r["error"])
             for r in conn.execute("SELECT id, path, mtime, scanned_at, detect_version, error FROM photos")}
    todo, seen = [], 0
    for path, mtime in iter_jpegs(root, log):
        if stopped():
            break
        seen += 1
        if progress and seen % 500 == 0:
            progress(0, 0)  # inventaire en cours (durée inconnue)
        k = known.get(path)
        up_to_date = k and k[2] and abs(k[1] - mtime) < 1e-3 and (k[4] or (k[3] or 0) >= DETECT_VERSION)
        if up_to_date and not rescan:
            continue
        if k:
            conn.execute("UPDATE photos SET mtime = ? WHERE id = ?", (mtime, k[0]))
            todo.append((k[0], path))
        else:
            cur = conn.execute("INSERT INTO photos (path, mtime) VALUES (?, ?)", (path, mtime))
            todo.append((cur.lastrowid, path))
        if limit and len(todo) >= limit:
            break
    conn.commit()
    t_list = time.perf_counter() - t_list
    log(f"JPEG trouvés : {seen} | à analyser : {len(todo)} | inventaire : {t_list:.1f} s")
    summary = {"found": seen, "todo": len(todo), "done": 0, "faces": 0, "errors": 0}
    if not todo or stopped():
        conn.close()
        return summary

    # 2) Lecture réseau en avance (threads) + calcul dans le fil principal
    n_done = n_faces = n_err = 0
    bytes_read = t_read = t_compute = 0.0
    t0 = time.perf_counter()
    pool = ThreadPoolExecutor(max_workers=io_threads)
    queue = deque()
    it = iter(todo)

    def refill():
        while len(queue) < io_threads * 2:
            try:
                pid, p = next(it)
            except StopIteration:
                return
            queue.append((pid, p, pool.submit(read_file, p, log)))

    try:
        refill()
        while queue:
            if stopped():
                log("Arrêté : progression enregistrée, relancez pour reprendre.")
                break
            pid, path, fut = queue.popleft()
            refill()
            data, dt = fut.result()
            t_read += dt
            if data is None:
                n_err += 1
                continue
            bytes_read += len(data)

            tc = time.perf_counter()
            img = engine.decode(data)
            if img is None:
                log(f"  ! JPEG illisible : {path}")
                conn.execute("UPDATE photos SET scanned_at = datetime('now'), error = 'decode' "
                             "WHERE id = ?", (pid,))
                n_err += 1
                continue
            faces = engine.analyze(img)
            t_compute += time.perf_counter() - tc

            save_faces(conn, pid, faces, img.shape[:2], img)
            n_done += 1
            n_faces += len(faces)
            if progress:
                progress(n_done, len(todo))
            if n_done % COMMIT_EVERY == 0:
                conn.commit()
                rate = n_done / (time.perf_counter() - t0)
                log(f"  {n_done}/{len(todo)} photos | {n_faces} visages | {rate:.1f} photos/s")
    except KeyboardInterrupt:
        log("\nInterrompu : progression enregistrée, relancez la même commande pour reprendre.")
    finally:
        conn.commit()
        pool.shutdown(wait=False, cancel_futures=True)
        conn.close()

    elapsed = time.perf_counter() - t0
    rate = n_done / elapsed if elapsed else 0
    log(f"Terminé : {n_done} photos, {n_faces} visages, {n_err} erreurs en {elapsed:.0f} s "
        f"({rate:.2f} photos/s)")
    summary.update(done=n_done, faces=n_faces, errors=n_err)
    if bench and n_done:
        mb = bytes_read / 1e6
        print("\n=== Banc d'essai ===")
        print(f"Taille moyenne JPEG     : {mb / n_done:.1f} Mo")
        print(f"Débit réseau mesuré     : {mb / t_read * io_threads:.0f} Mo/s (approx., {io_threads} lectures parallèles)")
        print(f"Calcul (décodage+IA)    : {t_compute / n_done * 1000:.0f} ms/photo")
        print(f"Visages par photo       : {n_faces / n_done:.2f}")
        for total in (50_000, 100_000):
            print(f"Estimation {total:>7} photos : {total / rate / 3600:.1f} h")
        goulot = "le calcul (CPU)" if t_compute > elapsed * 0.8 else "la lecture réseau"
        print(f"Goulot d'étranglement probable : {goulot}")
    return summary



def scan_contacts() -> None:
    """Crée un visage de référence pour chaque contact qui a une photo de profil."""
    conn = db.connect()
    engine = FaceEngine(score_threshold=0.6)  # photos de profil souvent petites : seuil plus bas
    conn.execute("DELETE FROM faces WHERE status = 'reference'")
    ok = none = 0
    for p in conn.execute("SELECT id, name, contact_photo FROM persons "
                          "WHERE contact_photo IS NOT NULL").fetchall():
        data, _ = read_file(p["contact_photo"])
        img = engine.decode(data) if data else None
        face = engine.largest_face(img) if img is not None else None
        if face is None:
            none += 1
            continue
        conn.execute(
            "INSERT INTO faces (photo_id, x, y, w, h, score, embedding, person_id, status) "
            "VALUES (NULL, ?, ?, ?, ?, ?, ?, ?, 'reference')",
            (face.x, face.y, face.w, face.h, face.score, face.embedding.tobytes(), p["id"]),
        )
        ok += 1
    conn.commit()
    conn.close()
    print(f"Visages de référence créés : {ok} | photos de contact sans visage détecté : {none}")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="Scan des visages FaceTag")
    ap.add_argument("root", nargs="?", help="Dossier racine des photos (ex. \\\\NAS\\photo ou D:\\Photos)")
    ap.add_argument("--bench", type=int, metavar="N", help="banc d'essai sur N photos non analysées")
    ap.add_argument("--limit", type=int, help="nombre max de photos à analyser")
    ap.add_argument("--io-threads", type=int, default=4, help="lectures réseau parallèles (défaut 4)")
    ap.add_argument("--contacts", action="store_true", help="analyser les photos de profil des contacts")
    ap.add_argument("--rescan", action="store_true",
                    help="réanalyser aussi les photos déjà analysées (visages confirmés/ignorés conservés)")
    a = ap.parse_args(argv)

    if a.contacts:
        scan_contacts()
    if a.root:
        scan(a.root, a.bench or a.limit, a.io_threads, bench=bool(a.bench), rescan=a.rescan)
    elif not a.contacts:
        ap.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
