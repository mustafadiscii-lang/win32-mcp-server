"""Content extractors for drawings, documents and data files.

Every extractor returns an :class:`Extraction` and never raises for a malformed
file — problems are reported in ``Extraction.error`` so one bad file cannot stop
an indexing run. Heavy/optional dependencies are imported lazily:

  - ``ezdxf``  DXF parsing (and DWG, after conversion)
  - ``pypdf``  PDF text
  - ODA File Converter (external exe) converts DWG → DXF. Without it DWG files
    are indexed by header metadata only (version, size, path).
"""

from __future__ import annotations

import html
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MAX_TEXT_CHARS = 200_000
MAX_ZIP_MEMBER_BYTES = 50 * 1024 * 1024
MAX_ZIP_TOTAL_BYTES = 200 * 1024 * 1024
MAX_PDF_PAGES = 60
MAX_PLAIN_BYTES = 2 * 1024 * 1024
ODA_TIMEOUT_SECONDS = 180

DWG_VERSIONS = {
    "AC1009": "R11/R12",
    "AC1012": "R13",
    "AC1014": "R14",
    "AC1015": "AutoCAD 2000",
    "AC1018": "AutoCAD 2004",
    "AC1021": "AutoCAD 2007",
    "AC1024": "AutoCAD 2010",
    "AC1027": "AutoCAD 2013",
    "AC1032": "AutoCAD 2018",
}

# DXF $INSUNITS codes most common in civil/architecture work.
INSUNITS = {0: "birimsiz", 1: "inch", 2: "feet", 4: "mm", 5: "cm", 6: "m", 7: "km"}

# Title-block attribute tags worth surfacing as structured fields.
_TITLE_TAG_HINTS = (
    "PROJE",
    "PROJECT",
    "ADA",
    "PARSEL",
    "PAFTA",
    "OLCEK",
    "ÖLÇEK",
    "SCALE",
    "TARIH",
    "TARİH",
    "DATE",
    "MIMAR",
    "MİMAR",
    "MUHENDIS",
    "MÜHENDİS",
    "CIZEN",
    "ÇİZEN",
    "KONTROL",
    "ONAY",
    "MAL_SAHIBI",
    "MAL SAHİBİ",
    "IL",
    "ILCE",
    "İLÇE",
    "MAHALLE",
    "REVIZYON",
    "REV",
    "NO",
    "DWG_NO",
    "TITLE",
    "BASLIK",
    "BAŞLIK",
)


@dataclass
class Extraction:
    doc_type: str
    text: str = ""
    meta: dict[str, Any] = field(default_factory=dict)
    error: str = ""


def extract(path: Path, *, oda_converter: str | None = None) -> Extraction:
    """Dispatch to the right extractor by file extension."""
    ext = path.suffix.lower()
    try:
        if ext == ".dwg":
            return extract_dwg(path, oda_converter=oda_converter)
        if ext == ".dxf":
            return extract_dxf(path)
        if ext == ".pdf":
            return extract_pdf(path)
        if ext in {".docx", ".docm"}:
            return extract_docx(path)
        if ext in {".pptx", ".pptm"}:
            return extract_pptx(path)
        if ext in {".xlsx", ".xlsm"}:
            return extract_xlsx(path)
        if ext in {".txt", ".csv", ".tsv", ".md", ".ncn", ".kml", ".gpx", ".json", ".xml"}:
            return extract_plain(path)
    except Exception as exc:  # malformed input must never abort a run
        return Extraction(doc_type=ext.lstrip(".") or "file", error=f"{type(exc).__name__}: {exc}")
    return Extraction(doc_type=ext.lstrip(".") or "file")


# ---------------------------------------------------------------------------
# Drawings
# ---------------------------------------------------------------------------


def read_dwg_version(path: Path) -> str:
    """Return the 6-byte DWG version tag (e.g. ``AC1032``) or ''."""
    with path.open("rb") as fh:
        head = fh.read(6)
    tag = head.decode("ascii", errors="replace")
    return tag if tag.startswith("AC") else ""


def find_oda_converter() -> str | None:
    """Locate ODA File Converter via env var, PATH or the default install folder."""
    env = os.getenv("WIN32_MCP_ODA_CONVERTER", "").strip()
    if env and Path(env).is_file():
        return env
    on_path = shutil.which("ODAFileConverter")
    if on_path:
        return on_path
    for base in (os.getenv("PROGRAMFILES", r"C:\Program Files"), os.getenv("PROGRAMFILES(X86)", "")):
        oda_dir = Path(base) / "ODA" if base else None
        if oda_dir is None or not oda_dir.is_dir():
            continue
        matches = sorted(oda_dir.glob("ODAFileConverter*/ODAFileConverter.exe"))
        if matches:
            return str(matches[-1])
    return None


def extract_dwg(path: Path, *, oda_converter: str | None = None) -> Extraction:
    tag = read_dwg_version(path)
    meta: dict[str, Any] = {"dwg_version": tag, "dwg_release": DWG_VERSIONS.get(tag, "bilinmiyor")}
    if not oda_converter:
        return Extraction(
            doc_type="dwg",
            meta={**meta, "content": "header_only"},
            error="ODA File Converter bulunamadi; yalnizca baslik bilgisi indekslendi",
        )

    with tempfile.TemporaryDirectory(prefix="datapool_dwg_") as tmp:
        in_dir = Path(tmp) / "in"
        out_dir = Path(tmp) / "out"
        in_dir.mkdir()
        out_dir.mkdir()
        # Copy so the converter never touches (or locks) the OneDrive original.
        local = in_dir / "drawing.dwg"
        shutil.copyfile(path, local)
        subprocess.run(
            [oda_converter, str(in_dir), str(out_dir), "ACAD2018", "DXF", "0", "1", "*.DWG"],
            check=False,
            capture_output=True,
            timeout=ODA_TIMEOUT_SECONDS,
        )
        dxf = out_dir / "drawing.dxf"
        if not dxf.is_file():
            return Extraction(doc_type="dwg", meta=meta, error="ODA File Converter DXF uretemedi")
        result = extract_dxf(dxf)
    result.doc_type = "dwg"
    result.meta = {**meta, **result.meta}
    return result


def extract_dxf(path: Path) -> Extraction:
    try:
        from ezdxf import recover
        from ezdxf.entities.mtext import MText
    except ImportError:
        return Extraction(doc_type="dxf", error="ezdxf kurulu degil (pip install win32-mcp-server[datapool])")

    doc, auditor = recover.readfile(str(path))
    header = doc.header
    meta: dict[str, Any] = {
        "dxf_version": doc.dxfversion,
        "units": INSUNITS.get(int(header.get("$INSUNITS", 0)), str(header.get("$INSUNITS", 0))),
        "layers": sorted(layer.dxf.name for layer in doc.layers)[:500],
        "blocks": sorted(b.name for b in doc.blocks if not b.name.startswith("*"))[:500],
        "layouts": [layout.name for layout in doc.layouts],
        "audit_errors": len(auditor.errors),
    }
    extmin, extmax = header.get("$EXTMIN"), header.get("$EXTMAX")
    if extmin is not None and extmax is not None:
        meta["extents"] = [round(float(v), 3) for v in (*tuple(extmin)[:2], *tuple(extmax)[:2])]

    texts: list[str] = []
    attributes: dict[str, list[str]] = {}
    counts: Counter[str] = Counter()
    for layout in doc.layouts:
        for entity in layout:
            kind = entity.dxftype()
            counts[kind] += 1
            if kind == "TEXT":
                texts.append(entity.dxf.text)
            elif isinstance(entity, MText):
                texts.append(str(entity.plain_text()))
            elif kind == "INSERT":
                for attrib in getattr(entity, "attribs", []):
                    value = str(attrib.dxf.text).strip()
                    if not value:
                        continue
                    texts.append(value)
                    tag = str(attrib.dxf.tag).strip()
                    if _is_title_tag(tag):
                        values = attributes.setdefault(tag, [])
                        if value not in values and len(values) < 20:
                            values.append(value)

    meta["entity_counts"] = dict(counts.most_common(25))
    if attributes:
        meta["title_block"] = attributes
    return Extraction(doc_type="dxf", text=_bound(_dedupe_lines(texts)), meta=meta)


def _is_title_tag(tag: str) -> bool:
    upper = tag.upper()
    return any(hint == upper or (len(hint) > 3 and hint in upper) for hint in _TITLE_TAG_HINTS)


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------


def extract_pdf(path: Path) -> Extraction:
    try:
        from pypdf import PdfReader
    except ImportError:
        return Extraction(doc_type="pdf", error="pypdf kurulu degil (pip install win32-mcp-server[datapool])")

    reader = PdfReader(str(path))
    pages = len(reader.pages)
    chunks = []
    for page in reader.pages[:MAX_PDF_PAGES]:
        chunks.append(page.extract_text() or "")
        if sum(len(c) for c in chunks) > MAX_TEXT_CHARS:
            break
    text = "\n".join(chunks)
    meta: dict[str, Any] = {"pages": pages}
    info = reader.metadata
    if info:
        meta["title"] = str(info.title or "")
        meta["author"] = str(info.author or "")
    if pages and not text.strip():
        meta["needs_ocr"] = True
    return Extraction(doc_type="pdf", text=_bound(text), meta=meta)


_W_TEXT = re.compile(r"<w:t(?:\s[^>]*)?>([^<]*)</w:t>")
_A_TEXT = re.compile(r"<a:t>([^<]*)</a:t>")
_SST_TEXT = re.compile(r"<t(?:\s[^>]*)?>([^<]*)</t>")
_SHEET_NAME = re.compile(r'<sheet\b[^>]*\bname="([^"]*)"')
_PARAGRAPH_END = re.compile(r"</w:p>|</a:p>")


def extract_docx(path: Path) -> Extraction:
    with zipfile.ZipFile(path) as zf:
        xml = _read_member(zf, "word/document.xml")
    text = "\n".join(_texts(_W_TEXT, para) for para in _PARAGRAPH_END.split(xml))
    return Extraction(doc_type="docx", text=_bound(_squash(text)))


def extract_pptx(path: Path) -> Extraction:
    with zipfile.ZipFile(path) as zf:
        slides = sorted(
            (n for n in zf.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
            key=lambda n: int(re.sub(r"\D", "", n.rsplit("/", 1)[-1])),
        )
        parts = []
        total = 0
        for idx, name in enumerate(slides, start=1):
            total += zf.getinfo(name).file_size
            if total > MAX_ZIP_TOTAL_BYTES:
                # Guard against decks (or zip bombs) whose slides add up to an unreasonable size.
                parts.append(f"[{len(slides) - idx + 1} slayt boyut siniri nedeniyle okunmadi]")
                break
            xml = _read_member(zf, name)
            body = "\n".join(_texts(_A_TEXT, para) for para in _PARAGRAPH_END.split(xml))
            parts.append(f"[Slayt {idx}]\n{_squash(body)}")
    return Extraction(doc_type="pptx", text=_bound("\n\n".join(parts)), meta={"slides": len(slides)})


def extract_xlsx(path: Path) -> Extraction:
    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())
        sheets = _SHEET_NAME.findall(_read_member(zf, "xl/workbook.xml")) if "xl/workbook.xml" in names else []
        strings = _SST_TEXT.findall(_read_member(zf, "xl/sharedStrings.xml")) if "xl/sharedStrings.xml" in names else []
    text = "\n".join(html.unescape(s) for s in strings if s.strip())
    return Extraction(
        doc_type="xlsx",
        text=_bound(text),
        meta={"sheets": [html.unescape(s) for s in sheets]},
    )


def extract_plain(path: Path) -> Extraction:
    with path.open("rb") as fh:
        raw = fh.read(MAX_PLAIN_BYTES)
    for encoding in ("utf-8-sig", "cp1254", "latin-1"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    meta: dict[str, Any] = {"encoding": encoding}
    if path.suffix.lower() in {".csv", ".tsv", ".txt", ".ncn"}:
        meta["lines"] = text.count("\n") + 1
    return Extraction(doc_type=path.suffix.lower().lstrip("."), text=_bound(text), meta=meta)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_member(zf: zipfile.ZipFile, name: str) -> str:
    info = zf.getinfo(name)
    if info.file_size > MAX_ZIP_MEMBER_BYTES:
        raise ValueError(f"{name} too large ({info.file_size} bytes)")
    return zf.read(name).decode("utf-8", errors="replace")


def _texts(pattern: re.Pattern[str], xml: str) -> str:
    return "".join(html.unescape(t) for t in pattern.findall(xml))


def _squash(text: str) -> str:
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


def _dedupe_lines(lines: list[str]) -> str:
    seen: set[str] = set()
    out = []
    for line in lines:
        clean = " ".join(str(line).split())
        if clean and clean not in seen:
            seen.add(clean)
            out.append(clean)
    return "\n".join(out)


def _bound(text: str) -> str:
    return text[:MAX_TEXT_CHARS]
