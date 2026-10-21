"""Base SQLite locale.

Le schéma est prévu pour 50 000+ photos :
- photos : un enregistrement par fichier, avec mtime pour ne rescanner que ce qui a changé
- faces  : un visage détecté = une zone + une empreinte (embedding SFace, 128 floats)
- persons: les personnes, liées si possible à une fiche Google Contacts
"""

import sqlite3
from pathlib import Path

from .paths import db_path

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS persons (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL,
    google_resource TEXT UNIQUE,          -- ex. "people/c123456789" ; NULL si saisi à la main
    contact_photo   TEXT,                 -- chemin local de la photo de profil, si présente
    updated_at      TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS photos (
    id          INTEGER PRIMARY KEY,
    path        TEXT NOT NULL UNIQUE,
    mtime       REAL NOT NULL,
    width       INTEGER,                  -- dimensions de l'image AFFICHÉE (orientation EXIF appliquée)
    height      INTEGER,
    scanned_at  TEXT,                     -- NULL = pas encore analysée
    written_at  TEXT                      -- dernière écriture des métadonnées
);

CREATE TABLE IF NOT EXISTS faces (
    id          INTEGER PRIMARY KEY,
    photo_id    INTEGER REFERENCES photos(id) ON DELETE CASCADE,  -- NULL = visage de référence (photo de contact)
    x REAL, y REAL, w REAL, h REAL,       -- zone normalisée 0..1, coin haut-gauche, sur l'image AFFICHÉE
                                          -- (convertie vers l'image stockée + centre MWG à l'écriture)
    score       REAL,                     -- confiance de la détection
    embedding   BLOB,                     -- vecteur SFace (float32, normalisé)
    person_id   INTEGER REFERENCES persons(id) ON DELETE SET NULL,
    status      TEXT NOT NULL DEFAULT 'unknown'
                CHECK (status IN ('unknown', 'suggested', 'confirmed', 'ignored', 'reference')),
    similarity  REAL                      -- score de la suggestion, si status = 'suggested'
);

-- Suggestions refusées : on ne repropose pas la même personne pour ce visage.
CREATE TABLE IF NOT EXISTS rejections (
    face_id   INTEGER REFERENCES faces(id) ON DELETE CASCADE,
    person_id INTEGER REFERENCES persons(id) ON DELETE CASCADE,
    PRIMARY KEY (face_id, person_id)
);

-- Noms suggérés pour chaque groupe d'inconnus (recalculés avec les suggestions).
CREATE TABLE IF NOT EXISTS group_hints (
    cluster_id INTEGER,
    person_id  INTEGER REFERENCES persons(id) ON DELETE CASCADE,
    score      REAL,
    rank       INTEGER,
    PRIMARY KEY (cluster_id, rank)
);

CREATE INDEX IF NOT EXISTS idx_faces_photo  ON faces(photo_id);
CREATE INDEX IF NOT EXISTS idx_faces_person ON faces(person_id);
CREATE INDEX IF NOT EXISTS idx_faces_status ON faces(status);
CREATE INDEX IF NOT EXISTS idx_photos_scan  ON photos(scanned_at);
"""


def connect(path: Path | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or db_path(), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _add_column(conn, table: str, column: str, ddl: str) -> None:
    cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def _migrate(conn: sqlite3.Connection) -> None:
    """Ajoute les colonnes apparues après la création initiale de la base."""
    _add_column(conn, "photos", "error", "error TEXT")              # fichier illisible : ne pas réessayer
    _add_column(conn, "photos", "needs_write", "needs_write INTEGER NOT NULL DEFAULT 0")
    _add_column(conn, "photos", "write_error", "write_error TEXT")
    _add_column(conn, "faces", "cluster_id", "cluster_id INTEGER")  # groupe de visages inconnus similaires
    # split_level = 99 : « Retirer du groupe », ce visage n'est plus jamais regroupé.
    _add_column(conn, "faces", "split_level", "split_level INTEGER NOT NULL DEFAULT 0")
    _add_column(conn, "faces", "split_tag", "split_tag TEXT")  # mémorise les « Scinder ce groupe »
    # « Pas un visage » (fausse détection) : status 'ignored' + false_positive = 1
    _add_column(conn, "faces", "false_positive", "false_positive INTEGER NOT NULL DEFAULT 0")
    # Apprentissage des fausses détections : fp_score = ressemblance avec une fausse détection connue
    # (à vérifier) ; fp_ok = 1 si vous avez confirmé que c'est bien un visage (ne plus le signaler).
    _add_column(conn, "faces", "fp_score", "fp_score REAL")
    _add_column(conn, "faces", "fp_ok", "fp_ok INTEGER NOT NULL DEFAULT 0")
    # Origine d'une personne : NULL (Google Contacts ou saisie dans FaceTag) ou 'file' (fichier .vcf/.csv)
    _add_column(conn, "persons", "source", "source TEXT")
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(photos)")}
    if "detect_version" not in cols:
        # Version des réglages de détection utilisés. Le seuil est passé de 0.8 à 0.6 le
        # 19.10.2026 à 11:32:39 UTC : les photos analysées avant seront réanalysées.
        conn.execute("ALTER TABLE photos ADD COLUMN detect_version INTEGER")
        conn.execute("UPDATE photos SET detect_version = CASE "
                     "WHEN scanned_at >= '2026-10-19 11:32:40' THEN 2 ELSE 1 END "
                     "WHERE scanned_at IS NOT NULL")
    _fix_blob_coords(conn)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_photos_write ON photos(needs_write)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_faces_cluster ON faces(cluster_id)")
    conn.commit()


def _fix_blob_coords(conn: sqlite3.Connection) -> None:
    """Corrige les coordonnées enregistrées en octets (float32 NumPy) par les premières versions."""
    import struct
    rows = conn.execute("SELECT id, x, y, w, h, score FROM faces WHERE typeof(x) = 'blob' "
                        "OR typeof(y) = 'blob' OR typeof(w) = 'blob' OR typeof(h) = 'blob' "
                        "OR typeof(score) = 'blob'").fetchall()

    def num(v):
        if isinstance(v, (bytes, bytearray)):
            return struct.unpack("<f", v)[0] if len(v) == 4 else struct.unpack("<d", v)[0]
        return v

    conn.executemany("UPDATE faces SET x = ?, y = ?, w = ?, h = ?, score = ? WHERE id = ?",
                     [(num(r["x"]), num(r["y"]), num(r["w"]), num(r["h"]), num(r["score"]), r["id"])
                      for r in rows])


def upsert_person(conn: sqlite3.Connection, name: str, google_resource: str,
                  contact_photo: str | None) -> int:
    """Crée ou met à jour une personne issue de Google Contacts. Retourne son id."""
    row = conn.execute(
        "SELECT id, name FROM persons WHERE google_resource = ?", (google_resource,)
    ).fetchone()
    if row:
        conn.execute(
            "UPDATE persons SET name = ?, contact_photo = COALESCE(?, contact_photo), "
            "updated_at = datetime('now') WHERE id = ?",
            (name, contact_photo, row["id"]),
        )
        if row["name"] != name:  # nom changé dans Google : réécrire les photos concernées
            mark_person_photos(conn, row["id"])
        return row["id"]
    cur = conn.execute(
        "INSERT INTO persons (name, google_resource, contact_photo) VALUES (?, ?, ?)",
        (name, google_resource, contact_photo),
    )
    return cur.lastrowid


def mark_person_photos(conn: sqlite3.Connection, person_id: int) -> None:
    conn.execute(
        "UPDATE photos SET needs_write = 1 WHERE id IN "
        "(SELECT photo_id FROM faces WHERE person_id = ? AND status = 'confirmed')",
        (person_id,),
    )
