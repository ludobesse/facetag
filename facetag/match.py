"""Étape 3 : suggestions de noms et regroupement des visages inconnus.

Principe :
- « Galerie » = visages que vous avez confirmés. En option (désactivée par défaut), les photos de
  profil Google servent de point de départ, uniquement pour les personnes sans visage confirmé :
  elles sont souvent petites (~96 px), anciennes ou sans visage, donc peu fiables.
- Pour chaque visage inconnu, on compare son empreinte SFace à la galerie (similarité cosinus).
  Si la meilleure personne dépasse SUGGEST_THRESHOLD, avec une avance suffisante sur la
  deuxième (MARGIN), le visage devient « suggéré ».
- Les visages restés inconnus sont regroupés entre eux (CLUSTER_THRESHOLD) pour pouvoir
  nommer un groupe entier d'un coup.

Seuils de départ, à ajuster à l'usage :
- OpenCV Zoo utilise 0.363 pour décider « même personne » avec SFace (sface.py).
  On prend un peu plus strict pour les suggestions, et nettement plus strict pour les groupes.

Usage : python -m facetag.match                      # galerie = visages confirmés
        python -m facetag.match --photos-contacts    # + photos de profil Google (personnes sans confirmé)
"""

import time

import numpy as np

from . import db

SUGGEST_THRESHOLD = 0.40
MARGIN = 0.05
CLUSTER_THRESHOLD = 0.50
CHUNK = 1000


def _emb(rows) -> np.ndarray:
    if not rows:
        return np.zeros((0, 128), np.float32)
    return np.vstack([np.frombuffer(r["embedding"], np.float32) for r in rows])


def _gallery(conn, use_contact_photos: bool):
    sql = ("SELECT person_id, embedding FROM faces "
           "WHERE status = 'confirmed' AND person_id IS NOT NULL AND photo_id IS NOT NULL")
    if use_contact_photos:
        sql += (" UNION ALL SELECT person_id, embedding FROM faces f WHERE f.status = 'reference' "
                "AND f.person_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM faces c "
                "WHERE c.person_id = f.person_id AND c.status = 'confirmed' AND c.photo_id IS NOT NULL)")
    return conn.execute(sql).fetchall()


def _per_person(E: np.ndarray, G: np.ndarray, g_col: np.ndarray, n_persons: int) -> np.ndarray:
    """(visages × personnes) : meilleure similarité de chaque visage avec chaque personne."""
    S = E @ G.T
    P = np.full((len(E), n_persons), -1.0, np.float32)
    for j in range(n_persons):
        P[:, j] = S[:, g_col == j].max(axis=1)
    return P


def suggest(conn, progress=None, use_contact_photos: bool = False) -> dict:
    gallery = _gallery(conn, use_contact_photos)
    cands = conn.execute(
        "SELECT id, embedding FROM faces "
        "WHERE photo_id IS NOT NULL AND status IN ('unknown', 'suggested')"
    ).fetchall()

    # On repart de zéro pour les suggestions (la galerie a pu évoluer).
    conn.execute("UPDATE faces SET status = 'unknown', person_id = NULL, similarity = NULL "
                 "WHERE status = 'suggested'")
    if not gallery or not cands:
        conn.commit()
        return {"gallery": len(gallery), "candidates": len(cands), "suggested": 0}

    G = _emb(gallery)
    g_person = np.array([r["person_id"] for r in gallery])
    persons = np.unique(g_person)
    p_index = {p: i for i, p in enumerate(persons)}
    g_col = np.array([p_index[p] for p in g_person])

    rejected = {}
    for r in conn.execute("SELECT face_id, person_id FROM rejections"):
        rejected.setdefault(r["face_id"], set()).add(r["person_id"])

    n_sugg = 0
    for start in range(0, len(cands), CHUNK):
        chunk = cands[start:start + CHUNK]
        P = _per_person(_emb(chunk), G, g_col, len(persons))  # meilleur score par personne
        for i, r in enumerate(chunk):
            for pid in rejected.get(r["id"], ()):
                if pid in p_index:
                    P[i, p_index[pid]] = -1.0
        order = np.argsort(-P, axis=1)
        best = P[np.arange(len(chunk)), order[:, 0]]
        second = P[np.arange(len(chunk)), order[:, 1]] if len(persons) > 1 else np.full(len(chunk), -1.0)
        ok = (best >= SUGGEST_THRESHOLD) & (best - second >= MARGIN)
        updates = [(int(persons[order[i, 0]]), float(best[i]), chunk[i]["id"])
                   for i in np.where(ok)[0]]
        # « AND status = 'unknown' » : ne jamais écraser un choix fait entre-temps dans l'application
        conn.executemany("UPDATE faces SET status = 'suggested', person_id = ?, similarity = ? "
                         "WHERE id = ? AND status = 'unknown'", updates)
        n_sugg += len(updates)
        if progress:
            progress(min(start + CHUNK, len(cands)), len(cands))
    conn.commit()
    return {"gallery": len(gallery), "candidates": len(cands), "suggested": n_sugg}


NO_GROUP = 99       # split_level : « Retirer du groupe », ce visage n'est plus regroupé


def _compatible(a: str, b: str) -> bool:
    """Deux visages issus de scissions différentes ne doivent plus être regroupés.
    Étiquettes : "" (jamais scindé), "12/0/", "12/0/1/"… compatibles si l'une prolonge l'autre
    (le « / » final évite qu'un préfixe numérique, ex. "1/" et "12/", soit pris pour un parent)."""
    return not a or not b or a.startswith(b) or b.startswith(a)


def cluster_unknown(conn) -> dict:
    """Regroupe les visages inconnus similaires (seuil CLUSTER_THRESHOLD, union-find par blocs),
    en respectant les scissions demandées par l'utilisateur."""
    rows = conn.execute("SELECT id, embedding, COALESCE(split_tag, '') tag FROM faces "
                        "WHERE photo_id IS NOT NULL AND status = 'unknown' AND fp_score IS NULL "
                        f"AND split_level < {NO_GROUP}").fetchall()
    conn.execute("UPDATE faces SET cluster_id = NULL")
    n = len(rows)
    if n < 2:
        conn.commit()
        return {"unknown": n, "groups": 0}
    E = _emb(rows)
    tags = [r["tag"] for r in rows]
    parent = np.arange(n)

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    # 1) liens entre visages compatibles ; 2) on n'unit pas deux groupes dont les étiquettes
    #    se contredisent (sinon un visage « pont » recollerait deux parties scindées)
    group_tags = {i: {tags[i]} if tags[i] else set() for i in range(n)}
    for start in range(0, n, CHUNK):
        S = E[start:start + CHUNK] @ E.T
        ii, jj = np.where(S >= CLUSTER_THRESHOLD)
        order = np.argsort(-S[ii, jj])                     # liens les plus sûrs d'abord
        for i, j in zip(ii[order] + start, jj[order]):
            if i >= j:
                continue
            ri, rj = find(i), find(j)
            if ri == rj:
                continue
            ti, tj = group_tags[ri], group_tags[rj]
            if all(_compatible(a, b) for a in ti for b in tj):
                keep, drop = min(ri, rj), max(ri, rj)
                parent[drop] = keep
                group_tags[keep] = ti | tj

    roots = np.array([find(i) for i in range(n)])
    counts = np.bincount(roots, minlength=n)
    updates = [(int(rows[roots[i]]["id"]), rows[i]["id"]) for i in range(n) if counts[roots[i]] >= 2]
    conn.executemany("UPDATE faces SET cluster_id = ? WHERE id = ?", updates)
    conn.commit()
    return {"unknown": n, "groups": int((counts >= 2).sum()), "grouped_faces": len(updates)}


def _two_means(E: np.ndarray, iters: int = 20) -> np.ndarray:
    """Coupe un ensemble d'empreintes en 2 (k-moyennes sphériques, départ sur les 2 visages
    les plus différents). Renvoie un tableau de 0/1."""
    S = E @ E.T
    a = int(np.argmin(S.mean(axis=1)))       # le plus « à part »
    b = int(np.argmin(S[a]))                 # le plus différent de lui
    C = E[[a, b]]
    lab = np.zeros(len(E), int)
    for _ in range(iters):
        new = np.argmax(E @ C.T, axis=1)
        if (new == lab).all() and _:
            break
        lab = new
        for k in (0, 1):
            if (lab == k).any():
                c = E[lab == k].mean(axis=0)
                C[k] = c / (np.linalg.norm(c) or 1)
    return lab


GROUP_HINT_MIN = 0.30   # score moyen minimal pour afficher un nom suggéré à côté d'un groupe
GROUP_HINTS = 3


def compute_group_hints(conn, use_contact_photos: bool = False) -> int:
    """Pour chaque groupe d'inconnus : les 3 personnes dont il se rapproche le plus.

    Score d'une personne = moyenne, sur les visages du groupe, de leur meilleure similarité avec
    les visages connus de cette personne. Un groupe entier peut ainsi pointer vers quelqu'un même
    si aucun visage, pris seul, n'atteint le seuil de suggestion individuelle.
    """
    conn.execute("DELETE FROM group_hints")
    gallery = _gallery(conn, use_contact_photos)
    rows = conn.execute("SELECT cluster_id, embedding FROM faces WHERE cluster_id IS NOT NULL "
                        "AND status = 'unknown' ORDER BY cluster_id").fetchall()
    if not gallery or not rows:
        conn.commit()
        return 0
    G = _emb(gallery)
    g_person = np.array([r["person_id"] for r in gallery])
    persons = np.unique(g_person)
    g_col = np.searchsorted(persons, g_person)
    clusters = np.array([r["cluster_id"] for r in rows])
    P = np.vstack([_per_person(_emb(rows[s:s + CHUNK]), G, g_col, len(persons))
                   for s in range(0, len(rows), CHUNK)])
    inserts = []
    for cid in np.unique(clusters):
        mean = P[clusters == cid].mean(axis=0)
        for rank, j in enumerate(np.argsort(-mean)[:GROUP_HINTS]):
            inserts.append((int(cid), int(persons[j]), float(mean[j]), rank))
    conn.executemany("INSERT INTO group_hints (cluster_id, person_id, score, rank) VALUES (?, ?, ?, ?)",
                     inserts)
    conn.commit()
    return len(np.unique(clusters))


def split_group(conn, cluster_id: int, use_contact_photos: bool = False) -> dict:
    """« Scinder ce groupe » : coupe le groupe en deux sous-groupes de visages qui se ressemblent,
    mémorise la coupure, puis recalcule les groupes. Répétable si un sous-groupe reste mélangé."""
    rows = conn.execute("SELECT id, embedding, COALESCE(split_tag, '') tag FROM faces "
                        "WHERE cluster_id = ?", (cluster_id,)).fetchall()
    if len(rows) < 2:
        return {"groups": 0, "isolated": len(rows), "faces": len(rows)}
    lab = _two_means(_emb(rows))
    conn.executemany("UPDATE faces SET split_tag = ? WHERE id = ?",
                     [(f"{r['tag'] or f'{cluster_id}/'}{int(l)}/", r["id"]) for r, l in zip(rows, lab)])
    cluster_unknown(conn)
    compute_group_hints(conn, use_contact_photos)
    ids = [r["id"] for r in rows]
    q = ",".join("?" * len(ids))
    res = conn.execute(f"SELECT cluster_id, COUNT(*) n FROM faces WHERE id IN ({q}) GROUP BY cluster_id",
                       ids).fetchall()
    return {"groups": sum(1 for r in res if r["cluster_id"] is not None),
            "isolated": sum(r["n"] for r in res if r["cluster_id"] is None),
            "faces": len(ids),
            "largest": max((r for r in res if r["cluster_id"] is not None),
                           key=lambda r: r["n"], default={"cluster_id": None})["cluster_id"]}


def remove_from_group(conn, face_ids: list[int]) -> None:
    """« Retirer du groupe » : ces visages ne seront plus regroupés (ils passent dans « isolés »)."""
    if not face_ids:
        return
    q = ",".join("?" * len(face_ids))
    conn.execute(f"UPDATE faces SET split_level = {NO_GROUP}, cluster_id = NULL WHERE id IN ({q})",
                 face_ids)
    conn.commit()


FP_THRESHOLD = 0.60   # ressemblance avec une fausse détection connue -> « fausse détection probable »


def flag_probable_false(conn) -> int:
    """Apprend de vos « Pas un visage » : les visages inconnus qui ressemblent fortement à une
    fausse détection déjà signalée (même statue, même motif, même affiche…) sont mis de côté
    dans « Fausses détections probables », pour vérification. Rien n'est ignoré sans vous."""
    conn.execute("UPDATE faces SET fp_score = NULL WHERE fp_score IS NOT NULL")
    fps = conn.execute("SELECT embedding FROM faces WHERE status = 'ignored' AND false_positive = 1").fetchall()
    rows = conn.execute("SELECT id, embedding FROM faces WHERE photo_id IS NOT NULL "
                        "AND status = 'unknown' AND fp_ok = 0").fetchall()
    if not fps or not rows:
        conn.commit()
        return 0
    F = _emb(fps)
    updates = []
    for s in range(0, len(rows), CHUNK):
        chunk = rows[s:s + CHUNK]
        best = (_emb(chunk) @ F.T).max(axis=1)
        updates += [(float(b), r["id"]) for r, b in zip(chunk, best) if b >= FP_THRESHOLD]
    conn.executemany("UPDATE faces SET fp_score = ? WHERE id = ? AND status = 'unknown'", updates)
    conn.commit()
    return len(updates)


def run(progress=None, use_contact_photos: bool = False) -> str:
    conn = db.connect()
    t = time.perf_counter()
    s = suggest(conn, progress, use_contact_photos)
    n_fp = flag_probable_false(conn)
    c = cluster_unknown(conn)
    compute_group_hints(conn, use_contact_photos)
    conn.close()
    return (f"Galerie : {s['gallery']} visages connus | analysés : {s['candidates']} | "
            f"suggestions : {s['suggested']} | groupes d'inconnus : {c['groups']} "
            f"({c.get('grouped_faces', 0)} visages) | fausses détections probables : {n_fp} | "
            f"{time.perf_counter() - t:.1f} s")


if __name__ == "__main__":
    import sys
    print(run(use_contact_photos="--photos-contacts" in sys.argv))
