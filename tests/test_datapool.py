from __future__ import annotations

import zipfile
from typing import TYPE_CHECKING

import pytest

from win32_mcp_server.datapool.classify import classify, extract_fields, fold, project_key
from win32_mcp_server.datapool.cli import main as cli_main
from win32_mcp_server.datapool.extractors import extract, read_dwg_version
from win32_mcp_server.datapool.scanner import ScanOptions, scan
from win32_mcp_server.datapool.store import DataPool, to_fts_query

if TYPE_CHECKING:
    from pathlib import Path


def _write_docx(path: Path, text: str) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(
            "word/document.xml", f"<w:document><w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>"
        )


def _write_pptx(path: Path, slides: list[str]) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        for idx, text in enumerate(slides, start=1):
            zf.writestr(f"ppt/slides/slide{idx}.xml", f"<p:sld><a:p><a:r><a:t>{text}</a:t></a:r></a:p></p:sld>")


def _write_dxf(path: Path) -> None:
    ezdxf = pytest.importorskip("ezdxf")
    doc = ezdxf.new("R2010")
    doc.layers.add("MIMARI_DUVAR")
    block = doc.blocks.new("ANTET")
    block.add_attdef("PROJE_ADI", (0, 0))
    block.add_attdef("ADA", (0, 5))
    msp = doc.modelspace()
    msp.add_text("ZEMIN KAT PLANI", dxfattribs={"layer": "MIMARI_DUVAR"})
    insert = msp.add_blockref("ANTET", (100, 0))
    insert.add_auto_attribs({"PROJE_ADI": "Deniz Konutlari", "ADA": "123"})
    msp.add_line((0, 0), (10, 0))
    doc.saveas(path)


@pytest.fixture
def archive(tmp_path: Path) -> Path:
    root = tmp_path / "OneDrive"
    (root / "Deniz Konutlari" / "Mimari").mkdir(parents=True)
    (root / "Deniz Konutlari" / "Zemin Etüdü").mkdir(parents=True)
    (root / "Teklifler").mkdir()
    _write_dxf(root / "Deniz Konutlari" / "Mimari" / "zemin_kat.dxf")
    _write_docx(root / "Deniz Konutlari" / "Zemin Etüdü" / "rapor.docx", "Sondaj SK-1 derinlik 15 m, 123 ada 45 parsel")
    _write_pptx(root / "Teklifler" / "2024_otel_sunumu.pptx", ["Otel Projesi", "Toplam bedel 1.250.000 TL"])
    (root / "Deniz Konutlari" / "olcum_noktalari.csv").write_text("NN,Y,X\n1,500000.12,4500000.55\n", encoding="utf-8")
    (root / "Deniz Konutlari" / "~$kilit.docx").write_bytes(b"lock")
    (root / "Deniz Konutlari" / "eski.dwg").write_bytes(b"AC1032" + b"\x00" * 64)
    return root


def test_fold_handles_turkish() -> None:
    assert fold("ETÜT Planı İmar Şişli Ğ") == "etut plani imar sisli g"


def test_classify_prefers_path_keywords() -> None:
    assert classify("Proje A/Zemin Etüdü/rapor.pdf", ".pdf")[0] == "etut_plani"
    assert classify("Teklifler/fiyat_teklifi.docx", ".docx")[0] == "teklif_sunumu"
    assert classify("Proje A/vaziyet.dwg", ".dwg")[0] == "proje"
    assert classify("x/olcumler.xlsx", ".xlsx")[0] == "veri"
    assert classify("x/y.zip", ".zip")[0] == "diger"


def test_project_key_and_fields() -> None:
    assert project_key("Deniz/Mimari/a.dwg") == "Deniz"
    assert project_key("Deniz/Mimari/a.dwg", depth=2) == "Deniz/Mimari"
    assert project_key("a.dwg") == "(kok)"
    fields = extract_fields("Tapu: 123 ada 45 parsel, 2023 yili", "x.pdf")
    assert fields["ada_parsel"] == ["123/45"]
    assert fields["yillar"] == ["2023"]


def test_fts_query_is_injection_safe() -> None:
    assert to_fts_query('etüt" OR *') == '"etüt" AND "OR"*'
    assert to_fts_query("  ") == ""


def test_dwg_without_converter_indexes_header(tmp_path: Path) -> None:
    path = tmp_path / "a.dwg"
    path.write_bytes(b"AC1027" + b"\x00" * 10)
    assert read_dwg_version(path) == "AC1027"
    result = extract(path, oda_converter=None)
    assert result.meta["dwg_release"] == "AutoCAD 2013"
    assert "ODA" in result.error


def test_malformed_file_does_not_raise(tmp_path: Path) -> None:
    path = tmp_path / "broken.docx"
    path.write_bytes(b"not a zip")
    result = extract(path)
    assert result.error


def test_scan_search_annotate_roundtrip(archive: Path, tmp_path: Path) -> None:
    with DataPool(tmp_path / "pool.sqlite") as pool:
        report = scan(pool, [archive], ScanOptions(workers=1, use_processes=False, oda_converter=""))
        assert report.indexed == 5
        assert report.errors == 0

        dxf = pool.search("zemin kat plani", ext="dxf")
        assert len(dxf) == 1
        record = pool.get(dxf[0]["id"])
        assert record is not None
        assert record["category"] == "proje"
        assert record["project"] == "Deniz Konutlari"
        assert "MIMARI_DUVAR" in record["meta"]["layers"]
        assert record["meta"]["title_block"]["ADA"] == ["123"]

        etut = pool.search("sondaj")
        assert [r["category"] for r in etut] == ["etut_plani"]
        assert pool.get(etut[0]["id"])["meta"]["fields"]["ada_parsel"] == ["123/45"]  # type: ignore[index]

        assert [r["category"] for r in pool.search("bedel")] == ["teklif_sunumu"]
        assert pool.search("etüd", category="etut_plani")  # diacritic-insensitive, matches "Etüdü"

        pending = pool.pending_reviews(limit=50)
        assert len(pending) == 5
        with pool.transaction():
            pool.annotate(etut[0]["id"], summary="SK-1 sondaj logu", tags=["zemin", "sondaj"], reviewed_by="ajan-1")
        assert len(pool.pending_reviews(limit=50)) == 4
        assert pool.search("logu")[0]["summary"] == "SK-1 sondaj logu"

        stats = pool.stats()
        assert stats["files"] == 5
        assert stats["reviewed"] == 1

        again = scan(pool, [archive], ScanOptions(workers=1, use_processes=False, oda_converter=""))
        # Unchanged files are skipped, including the header-only ("partial") DWG.
        assert again.unchanged == 5
        assert again.indexed == 0

        retry = scan(pool, [archive], ScanOptions(workers=1, use_processes=False, oda_converter="", retry_partial=True))
        assert retry.indexed == 1  # only the partial DWG is re-extracted

        (archive / "Teklifler" / "2024_otel_sunumu.pptx").unlink()
        third = scan(pool, [archive], ScanOptions(workers=1, use_processes=False, oda_converter=""))
        assert third.pruned == 1
        assert pool.stats()["files"] == 4


def test_scan_respects_max_files(archive: Path, tmp_path: Path) -> None:
    with DataPool(tmp_path / "pool.sqlite") as pool:
        report = scan(pool, [archive], ScanOptions(workers=1, use_processes=False, max_files=2, oda_converter=""))
        assert report.stopped_early
        assert report.indexed == 2
        assert report.pruned == 0


def test_cli_index_and_export(archive: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = str(tmp_path / "cli.sqlite")
    assert cli_main(["--db", db, "index", str(archive), "--workers", "1", "-q"]) == 0
    assert '"indexed": 5' in capsys.readouterr().out
    out = tmp_path / "pool.csv"
    assert cli_main(["--db", db, "export", str(out)]) == 0
    assert "Deniz Konutlari" in out.read_text(encoding="utf-8-sig")


def test_cli_index_uses_configured_roots(
    archive: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("WIN32_MCP_DATAPOOL_ROOTS", str(archive))
    db = str(tmp_path / "env.sqlite")
    assert cli_main(["--db", db, "index", "--workers", "1", "-q"]) == 0
    assert '"indexed": 5' in capsys.readouterr().out


def test_max_files_moves_past_partial_files(tmp_path: Path) -> None:
    root = tmp_path / "arsiv"
    root.mkdir()
    for idx in range(5):
        (root / f"plan_{idx}.dwg").write_bytes(b"AC1032" + b"\x00" * 64)
    (root / "notlar.txt").write_text("santiye notu", encoding="utf-8")
    opts = ScanOptions(workers=1, use_processes=False, max_files=3, oda_converter="")
    with DataPool(tmp_path / "pool.sqlite") as pool:
        reports = [scan(pool, [root], opts) for _ in range(3)]
        assert [r.stopped_early for r in reports] == [True, False, False]
        assert pool.stats()["files"] == 6
        assert pool.search("santiye")


def test_unreadable_folder_does_not_prune(archive: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    opts = ScanOptions(workers=1, use_processes=False, oda_converter="")
    with DataPool(tmp_path / "pool.sqlite") as pool:
        scan(pool, [archive], opts)
        etut = pool.search("sondaj")[0]
        with pool.transaction():
            pool.annotate(etut["id"], summary="incelendi")

        real_scandir = os.scandir
        blocked = str(archive / "Deniz Konutlari" / "Zemin Etüdü")

        def flaky_scandir(path: object) -> object:
            if str(path) == blocked:
                raise PermissionError(13, "Erisim engellendi", blocked)
            return real_scandir(path)  # type: ignore[arg-type]

        monkeypatch.setattr(os, "scandir", flaky_scandir)
        report = scan(pool, [archive], opts)
        assert report.unreadable_dirs == 1
        assert report.pruned == 0
        assert pool.get(etut["id"])["summary"] == "incelendi"  # type: ignore[index]


def test_subfolder_scan_uses_allowed_root(archive: Path, tmp_path: Path) -> None:
    sub = archive / "Deniz Konutlari"
    opts = ScanOptions(workers=1, use_processes=False, oda_converter="", base_roots=(archive,))
    with DataPool(tmp_path / "pool.sqlite") as pool:
        scan(pool, [archive], opts)
        before = {r["project"] for r in pool.export_rows()}
        report = scan(
            pool,
            [sub],
            ScanOptions(workers=1, use_processes=False, oda_converter="", base_roots=(archive,), force=True),
        )
        assert report.pruned == 0
        assert {r["project"] for r in pool.export_rows()} == before
        assert pool.search("sondaj")[0]["project"] == "Deniz Konutlari"

        (sub / "olcum_noktalari.csv").unlink()
        (archive / "Teklifler" / "2024_otel_sunumu.pptx").unlink()
        pruned = scan(pool, [sub], opts)
        # Only the scanned sub-folder is pruned; the deleted file elsewhere waits for a full scan.
        assert pruned.pruned == 1
        assert pool.search("bedel")


def test_missing_root_does_not_abort(archive: Path, tmp_path: Path) -> None:
    with DataPool(tmp_path / "pool.sqlite") as pool:
        report = scan(pool, [tmp_path / "yok", archive], ScanOptions(workers=1, use_processes=False, oda_converter=""))
        assert report.missing_roots == [str((tmp_path / "yok").resolve())]
        assert report.indexed == 5


def test_symlinked_folder_is_not_followed(archive: Path, tmp_path: Path) -> None:
    outside = tmp_path / "disarida"
    outside.mkdir()
    (outside / "gizli.txt").write_text("gizli belge", encoding="utf-8")
    try:
        (archive / "baglanti").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks not available")
    with DataPool(tmp_path / "pool.sqlite") as pool:
        scan(pool, [archive], ScanOptions(workers=1, use_processes=False, oda_converter=""))
        assert not pool.search("gizli")


def test_pptx_total_size_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from win32_mcp_server.datapool import extractors

    path = tmp_path / "buyuk.pptx"
    _write_pptx(path, ["Birinci", "Ikinci", "Ucuncu"])
    monkeypatch.setattr(extractors, "MAX_ZIP_TOTAL_BYTES", 120)
    result = extract(path)
    assert "Birinci" in result.text
    assert "Ucuncu" not in result.text
    assert "boyut siniri" in result.text
