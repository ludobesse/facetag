"""Actions de l'utilisateur sur les visages (utilisées par l'application).

Chaque action marque les photos concernées « à écrire » (needs_write) pour l'étape 5.
"""

import sqlite3


def _mark(conn, face_ids):
    q = ",".join("?" * len(face_ids))
    conn.execute(f"UPDATE photos SET needs_write = 1 WHERE id IN "
                 f"(SELECT photo_id FROM faces WHERE id IN ({q}))", face_ids)


def confirm_suggestions(conn: sqlite3.Connection, face_ids: list[int]) -> None:
    """Valide la personne suggérée."""
    if not face_ids:
        return
    q = ",".join("?" * len(face_ids))
    conn.execute(f"UPDATE faces SET status = 'confirmed', cluster_id = NULL "
                 f"WHERE id IN ({q}) AND person_id IS NOT NULL", face_ids)
    _mark(conn, face_ids)
    conn.commit()


def assign(conn: sqlite3.Connection, face_ids: list[int], person_id: int) -> None:
    """Attribue (et confirme) une personne à des visages."""
    if not face_ids:
        return
    q = ",".join("?" * len(face_ids))
    _mark(conn, face_ids)  # avant : couvre aussi les photos où le nom change
    conn.execute(f"UPDATE faces SET status = 'confirmed', person_id = ?, similarity = NULL, "
                 f"cluster_id = NULL, fp_score = NULL WHERE id IN ({q})", [person_id, *face_ids])
    conn.commit()


def reject(conn: sqlite3.Connection, face_ids: list[int]) -> None:
    """Refuse la suggestion (ou retire un nom confirmé) : le visage redevient inconnu."""
    if not face_ids:
        return
    q = ",".join("?" * len(face_ids))
    conn.execute(f"INSERT OR IGNORE INTO rejections (face_id, person_id) "
                 f"SELECT id, person_id FROM faces WHERE id IN ({q}) AND person_id IS NOT NULL",
                 face_ids)
    _mark(conn, face_ids)
    conn.execute(f"UPDATE faces SET status = 'unknown', person_id = NULL, similarity = NULL "
                 f"WHERE id IN ({q})", face_ids)
    conn.commit()


def ignore(conn: sqlite3.Connection, face_ids: list[int], false_positive: bool = False) -> None:
    """Ignorer : personne qu'on ne veut pas nommer.
    false_positive=True : « Pas un visage » (fausse détection : objet, motif, statue…).
    Dans les deux cas, la zone n'est plus proposée, même si la photo est réanalysée."""
    if not face_ids:
        return
    q = ",".join("?" * len(face_ids))
    _mark(conn, face_ids)
    conn.execute(f"UPDATE faces SET status = 'ignored', false_positive = ?, person_id = NULL, "
                 f"similarity = NULL, cluster_id = NULL, fp_score = NULL WHERE id IN ({q})",
                 [int(false_positive), *face_ids])
    conn.commit()


def not_a_face(conn: sqlite3.Connection, face_ids: list[int]) -> None:
    ignore(conn, face_ids, false_positive=True)


def is_a_face(conn: sqlite3.Connection, face_ids: list[int]) -> None:
    """« C'est bien un visage » : ne plus le signaler comme fausse détection probable."""
    if not face_ids:
        return
    q = ",".join("?" * len(face_ids))
    conn.execute(f"UPDATE faces SET fp_score = NULL, fp_ok = 1 WHERE id IN ({q})", face_ids)
    conn.commit()


def restore(conn: sqlite3.Connection, face_ids: list[int]) -> None:
    """Annule « ignorer » : le visage redevient inconnu."""
    if not face_ids:
        return
    q = ",".join("?" * len(face_ids))
    conn.execute(f"UPDATE faces SET status = 'unknown', false_positive = 0 "
                 f"WHERE id IN ({q}) AND status = 'ignored'", face_ids)
    conn.commit()


def get_or_create_person(conn: sqlite3.Connection, name: str) -> int:
    """Personne existante (nom exact, sans casse) ou nouvelle personne hors Google Contacts."""
    name = " ".join(name.split())
    row = conn.execute("SELECT id FROM persons WHERE name = ? COLLATE NOCASE "
                       "ORDER BY google_resource IS NULL LIMIT 1", (name,)).fetchone()
    if row:
        return row["id"]
    cur = conn.execute("INSERT INTO persons (name) VALUES (?)", (name,))
    conn.commit()
    return cur.lastrowid
