"""État de la base FaceTag : ce qui a été fait, et ce qui reste à faire.

Lecture seule. Usage : python -m facetag.status
"""

from . import db
from .paths import db_path


def main() -> None:
    conn = db.connect()
    q = lambda sql: conn.execute(sql).fetchone()[0]

    print(f"Base : {db_path()}\n")

    print("Personnes (Google Contacts)")
    print(f"  total                       : {q('SELECT COUNT(*) FROM persons')}")
    n_ref = q("SELECT COUNT(DISTINCT person_id) FROM faces WHERE status = 'reference'")
    print(f"  avec visage de référence    : {n_ref}")

    print("\nPhotos")
    print(f"  inventoriées                : {q('SELECT COUNT(*) FROM photos')}")
    print(f"  analysées                   : {q('SELECT COUNT(*) FROM photos WHERE scanned_at IS NOT NULL AND error IS NULL')}")
    print(f"  en erreur (illisibles)      : {q('SELECT COUNT(*) FROM photos WHERE error IS NOT NULL')}")
    print(f"  avec au moins un visage     : {q('SELECT COUNT(DISTINCT photo_id) FROM faces WHERE photo_id IS NOT NULL')}")
    print(f"  métadonnées écrites         : {q('SELECT COUNT(*) FROM photos WHERE written_at IS NOT NULL')}")
    print(f"  en attente d'écriture       : {q('SELECT COUNT(*) FROM photos WHERE needs_write = 1')}")
    print(f"  à réanalyser (réglages)     : {q('SELECT COUNT(*) FROM photos WHERE scanned_at IS NOT NULL AND error IS NULL AND COALESCE(detect_version, 0) < 2')}")

    print("\nVisages détectés dans les photos")
    for status, label in [("unknown", "inconnus (à nommer)"), ("suggested", "suggérés (à confirmer)"),
                          ("confirmed", "confirmés")]:
        n = conn.execute("SELECT COUNT(*) FROM faces WHERE photo_id IS NOT NULL AND status = ?",
                         (status,)).fetchone()[0]
        print(f"  {label:<28}: {n}")
    n_ign = q("SELECT COUNT(*) FROM faces WHERE status = 'ignored' AND false_positive = 0")
    n_fp = q("SELECT COUNT(*) FROM faces WHERE status = 'ignored' AND false_positive = 1")
    print(f"  {'ignorés':<28}: {n_ign}")
    print(f"  {'fausses détections':<28}: {n_fp}")

    top = conn.execute(
        "SELECT p.name, COUNT(*) n FROM faces f JOIN persons p ON p.id = f.person_id "
        "WHERE f.photo_id IS NOT NULL AND f.status = 'confirmed' GROUP BY p.id ORDER BY n DESC LIMIT 10"
    ).fetchall()
    if top:
        print("\nPersonnes les plus présentes (visages confirmés)")
        for r in top:
            print(f"  {r['name']:<28}: {r['n']}")
    conn.close()


if __name__ == "__main__":
    main()
