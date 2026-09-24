"""Rule-based classification of files into pool categories.

Categories:
  - proje          Drawings and design deliverables (DWG/DXF, plans, sections)
  - etut_plani     Survey / site investigation plans (zemin etüdü, jeoloji, sondaj, halihazır)
  - teklif_sunumu  Offers, quotations and presentations
  - veri           Raw or tabular data (measurements, coordinates, spreadsheets)
  - diger          Anything that matched no rule
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import PurePath

CATEGORIES = ("proje", "etut_plani", "teklif_sunumu", "veri", "diger")

DRAWING_EXTS = {".dwg", ".dxf", ".dwf", ".dwfx", ".dgn"}
DATA_EXTS = {".xlsx", ".xlsm", ".xls", ".csv", ".tsv", ".txt", ".ncn", ".kml", ".gpx", ".json"}
PRESENTATION_EXTS = {".pptx", ".ppt"}

# Keywords are matched against the ASCII-folded, lower-cased path so "Etüt", "ETUD" and
# "etut" all hit the same rule. Order matters: the first category with a hit wins
# unless a later one has strictly more hits.
_KEYWORDS: dict[str, tuple[str, ...]] = {
    "etut_plani": (
        "etut",
        "etud",
        "zemin",
        "jeoloji",
        "jeolojik",
        "jeoteknik",
        "sondaj",
        "halihazir",
        "topograf",
        "aplikasyon",
        "survey",
        "site investigation",
        "imar durumu",
        "fizibilite",
    ),
    "teklif_sunumu": (
        "teklif",
        "sunum",
        "kesif",
        "fiyat",
        "maliyet",
        "hakedis",
        "ihale",
        "sozlesme",
        "presentation",
        "offer",
        "quotation",
        "proposal",
        "brosur",
    ),
    "veri": (
        "veri",
        "data",
        "olcum",
        "koordinat",
        "nokta",
        "metraj",
        "rapor",
        "tablo",
    ),
    "proje": (
        "proje",
        "project",
        "vaziyet",
        "kat plan",
        "kesit",
        "gorunus",
        "cephe",
        "mimari",
        "statik",
        "betonarme",
        "tesisat",
        "mekanik",
        "elektrik",
        "peyzaj",
        "detay",
        "avan",
        "uygulama",
        "ruhsat",
        "pafta",
    ),
}

_ADA_PARSEL_RE = re.compile(r"(\d{1,6})\s*ada\s*[,/-]?\s*(\d{1,6})\s*(?:no'?lu\s*)?parsel", re.IGNORECASE)
_YEAR_RE = re.compile(r"(?<!\d)(19[89]\d|20[0-4]\d)(?!\d)")


def fold(text: str) -> str:
    """Lower-case and strip Turkish/Latin diacritics (ı→i, ş→s, ğ→g, ü→u, ö→o, ç→c)."""
    text = text.replace("ı", "i").replace("İ", "i").replace("I", "i")
    normalized = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in normalized if not unicodedata.combining(ch)).lower()


def classify(rel_path: str, ext: str, text: str = "") -> tuple[str, dict[str, int]]:
    """Return (category, per-category hit counts) for a file."""
    haystack = fold(rel_path.replace("_", " ").replace("-", " "))
    head = fold(text[:4000]) if text else ""

    scores: dict[str, int] = {}
    for category, words in _KEYWORDS.items():
        # Path hits weigh more than content hits: folder names are deliberate labels.
        score = sum(3 for w in words if w in haystack) + sum(1 for w in words if head and w in head)
        if score:
            scores[category] = score

    ext = ext.lower()
    if ext in DRAWING_EXTS:
        scores["proje"] = scores.get("proje", 0) + 2
    elif ext in PRESENTATION_EXTS:
        scores["teklif_sunumu"] = scores.get("teklif_sunumu", 0) + 2
    elif ext in DATA_EXTS:
        scores["veri"] = scores.get("veri", 0) + 2

    if not scores:
        return "diger", scores
    best = max(scores.items(), key=lambda item: item[1])[0]
    return best, scores


def project_key(rel_path: str, depth: int = 1) -> str:
    """Derive a project name from the first ``depth`` folders of the relative path."""
    parts = PurePath(rel_path.replace("\\", "/")).parts[:-1]
    if not parts:
        return "(kok)"
    return "/".join(parts[:depth])


def extract_fields(text: str, rel_path: str) -> dict[str, list[str]]:
    """Pull structured hints (ada/parsel, years) from text and path."""
    source = f"{rel_path}\n{text[:200_000]}"
    fields: dict[str, list[str]] = {}
    ada_parsel = sorted({f"{a}/{p}" for a, p in _ADA_PARSEL_RE.findall(fold(source))})
    if ada_parsel:
        fields["ada_parsel"] = ada_parsel[:50]
    years = sorted(set(_YEAR_RE.findall(source)))
    if years:
        fields["yillar"] = years[:20]
    return fields
