"""Command line for the data pool: ``win32-mcp-datapool index|search|stats|projects|export``."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .scanner import DEFAULT_EXTENSIONS, ScanOptions, ScanReport, onedrive_roots, scan
from .store import DataPool, default_db_path

if TYPE_CHECKING:
    from collections.abc import Sequence


def _print_json(value: Any) -> None:
    sys.stdout.write(json.dumps(value, indent=2, ensure_ascii=False, default=str) + "\n")


def _cmd_index(args: argparse.Namespace, pool: DataPool) -> int:
    roots = [Path(r) for r in args.roots] or onedrive_roots()
    if not roots:
        sys.stderr.write(
            "Klasor verilmedi ve OneDrive klasoru bulunamadi. Ornek: win32-mcp-datapool index D:\\Projeler\n"
        )
        return 2
    exts = frozenset(f".{e.lower().lstrip('.')}" for e in args.ext) if args.ext else DEFAULT_EXTENSIONS
    opts = ScanOptions(
        extensions=exts,
        workers=args.workers,
        hydrate=args.hydrate,
        force=args.force,
        prune=not args.no_prune,
        max_files=args.max_files,
        project_depth=args.project_depth,
        oda_converter=args.oda,
    )

    def progress(report: ScanReport, rel_path: str) -> None:
        if not args.quiet:
            done = report.indexed + report.cloud_only
            sys.stderr.write(f"\r[{done} islendi, {report.unchanged} degismemis] {rel_path[-80:]:<80}")
            sys.stderr.flush()

    report = scan(pool, roots, opts, progress)
    if not args.quiet:
        sys.stderr.write("\n")
    _print_json(report.as_dict())
    return 0


def _cmd_export(args: argparse.Namespace, pool: DataPool) -> int:
    out = Path(args.output)
    count = 0
    if out.suffix.lower() == ".csv":
        cols = [
            "id",
            "project",
            "category",
            "auto_category",
            "doc_type",
            "rel_path",
            "size",
            "status",
            "summary",
            "tags",
        ]
        with out.open("w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
            writer.writeheader()
            for count, row in enumerate(pool.export_rows(), start=1):  # noqa: B007
                writer.writerow(row)
    else:
        with out.open("w", encoding="utf-8") as fh:
            for count, row in enumerate(pool.export_rows(), start=1):  # noqa: B007
                fh.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    _print_json({"exported": count, "output": str(out)})
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="win32-mcp-datapool",
        description="OneDrive / proje arsivinden DWG, PDF, Office ve veri dosyalarini SQLite veri havuzuna indeksler.",
    )
    parser.add_argument("--db", default=None, help=f"Veritabani yolu (varsayilan: {default_db_path()})")
    sub = parser.add_subparsers(dest="command", required=True)

    p_index = sub.add_parser("index", help="Klasorleri tara ve havuzu guncelle")
    p_index.add_argument("roots", nargs="*", help="Taranacak klasorler (bos: OneDrive klasorleri)")
    p_index.add_argument("--workers", type=int, default=ScanOptions().workers, help="Paralel ajan/isci sayisi")
    p_index.add_argument("--ext", action="append", default=[], help="Sadece bu uzantilar (tekrarlanabilir)")
    p_index.add_argument("--hydrate", action="store_true", help="Sadece bulutta olan OneDrive dosyalarini indir")
    p_index.add_argument("--force", action="store_true", help="Degismemis dosyalari da yeniden isle")
    p_index.add_argument("--no-prune", action="store_true", help="Silinmis dosyalarin kayitlarini tutmaya devam et")
    p_index.add_argument("--max-files", type=int, default=0, help="Bu calismada en fazla N dosya isle")
    p_index.add_argument("--project-depth", type=int, default=1, help="Proje adi icin kac klasor seviyesi")
    p_index.add_argument("--oda", default=None, help="ODAFileConverter.exe yolu (DWG icerigi icin)")
    p_index.add_argument("-q", "--quiet", action="store_true")

    p_search = sub.add_parser("search", help="Havuzda tam metin arama")
    p_search.add_argument("query", nargs="?", default="")
    p_search.add_argument("--category", default="")
    p_search.add_argument("--project", default="")
    p_search.add_argument("--ext", default="")
    p_search.add_argument("--limit", type=int, default=20)

    p_show = sub.add_parser("show", help="Tek bir kaydin tum detaylari")
    p_show.add_argument("id", type=int)

    sub.add_parser("stats", help="Havuz istatistikleri")
    sub.add_parser("projects", help="Proje bazinda ozet")

    p_export = sub.add_parser("export", help="Havuzu JSONL veya CSV olarak disari aktar")
    p_export.add_argument("output", help="Cikti dosyasi (.jsonl veya .csv)")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    with DataPool(args.db) as pool:
        if args.command == "index":
            return _cmd_index(args, pool)
        if args.command == "search":
            _print_json(
                pool.search(args.query, category=args.category, project=args.project, ext=args.ext, limit=args.limit)
            )
            return 0
        if args.command == "show":
            record = pool.get(args.id, max_text_chars=20_000)
            _print_json(record or {"error": f"kayit yok: {args.id}"})
            return 0 if record else 1
        if args.command == "stats":
            _print_json(pool.stats())
            return 0
        if args.command == "projects":
            _print_json(pool.projects())
            return 0
        if args.command == "export":
            return _cmd_export(args, pool)
    return 2


def entry_point() -> None:
    sys.exit(main())


if __name__ == "__main__":
    entry_point()
