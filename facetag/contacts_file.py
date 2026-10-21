"""Import de contacts depuis un FICHIER exporté (aucun identifiant Google nécessaire).

Google Contacts → Exporter → « vCard » (.vcf) ou « Google CSV » (.csv).
Fonctionne aussi avec les exports d'iPhone, d'Outlook, etc. (vCard).

Limite : c'est une copie, pas une connexion. Les nouveaux contacts n'apparaissent qu'après un
nouvel export, et FaceTag ne peut pas créer de contact ni changer leur photo chez Google.
"""

import base64
import csv
import hashlib
import io
import re
from pathlib import Path

import requests

from . import db
from .paths import contact_photos_dir


def _unfold(text: str) -> list[str]:
    """vCard : une ligne qui commence par un espace ou une tabulation continue la précédente."""
    lines: list[str] = []
    for raw in text.splitlines():
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def _unescape(v: str) -> str:
    return v.replace("\\,", ",").replace("\\;", ";").replace("\\n", " ").replace("\\\\", "\\").strip()


def parse_vcf(text: str) -> list[dict]:
    people, cur = [], None
    for line in _unfold(text):
        if not line:
            continue
        key, _, value = line.partition(":")
        name, *params = key.split(";")
        name = name.split(".")[-1].upper()            # « item1.EMAIL » -> « EMAIL »
        params_u = [p.upper() for p in params]
        if name == "BEGIN" and value.upper() == "VCARD":
            cur = {"name": "", "n": "", "photo": None}
        elif name == "END" and cur is not None:
            full = cur["name"] or cur["n"]
            if full:
                people.append({"name": full, "photo": cur["photo"]})
            cur = None
        elif cur is None:
            continue
        elif name == "FN":
            cur["name"] = _unescape(value)
        elif name == "N":
            parts = [_unescape(p) for p in value.split(";")] + [""] * 5
            family, given, middle = parts[0], parts[1], parts[2]
            cur["n"] = " ".join(p for p in (given, middle, family) if p)
        elif name == "PHOTO":
            if value.startswith("data:") and "," in value:          # vCard 4 : data URI
                cur["photo"] = ("b64", value.split(",", 1)[1])
            elif any(p in ("ENCODING=B", "ENCODING=BASE64") for p in params_u) or "BASE64" in " ".join(params_u):
                cur["photo"] = ("b64", value)
            elif value.startswith("http"):
                cur["photo"] = ("url", value)
    return people


def parse_csv(text: str) -> list[dict]:
    reader = csv.DictReader(io.StringIO(text))
    people = []
    for row in reader:
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
        full = row.get("name") or row.get("full name") or " ".join(
            p for p in (row.get("first name") or row.get("given name"),
                        row.get("middle name") or row.get("additional name"),
                        row.get("last name") or row.get("family name")) if p)
        if not full:
            continue
        photo = row.get("photo") or ""
        people.append({"name": " ".join(full.split()),
                       "photo": ("url", photo) if photo.startswith("http") else None})
    return people


def _save_photo(name: str, photo) -> str | None:
    if not photo:
        return None
    kind, value = photo
    try:
        data = base64.b64decode(re.sub(r"\s+", "", value)) if kind == "b64" else \
            requests.get(value, timeout=15).content
    except (ValueError, requests.RequestException):
        return None
    if len(data) < 100:
        return None
    dest = contact_photos_dir() / ("file_" + hashlib.sha1(name.casefold().encode()).hexdigest()[:16] + ".jpg")
    dest.write_bytes(data)
    return str(dest)


def import_file(path: str, download_photos: bool = True, log=print) -> dict:
    raw = Path(path).read_bytes()
    text = raw.decode("utf-8-sig", errors="replace")
    people = parse_csv(text) if path.lower().endswith(".csv") else parse_vcf(text)
    conn = db.connect()
    added = updated = photos = 0
    for p in people:
        row = conn.execute("SELECT id, contact_photo FROM persons WHERE name = ? COLLATE NOCASE "
                           "ORDER BY google_resource IS NULL LIMIT 1", (p["name"],)).fetchone()
        photo = _save_photo(p["name"], p["photo"]) if download_photos else None
        photos += bool(photo)
        if row:
            if photo and not row["contact_photo"]:
                conn.execute("UPDATE persons SET contact_photo = ? WHERE id = ?", (photo, row["id"]))
            updated += 1
        else:
            conn.execute("INSERT INTO persons (name, contact_photo, source) VALUES (?, ?, 'file')",
                         (p["name"], photo))
            added += 1
    conn.commit()
    conn.close()
    msg = (f"Fichier lu : {len(people)} contact(s) | nouveaux : {added} | déjà connus : {updated} | "
           f"photos : {photos}")
    log(msg)
    return {"read": len(people), "added": added, "updated": updated, "photos": photos, "message": msg}
