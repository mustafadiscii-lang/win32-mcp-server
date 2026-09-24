"""Folder walker that feeds files through extractors into the pool, in parallel workers."""

from __future__ import annotations

import logging
import os
import stat
import time
from concurrent.futures import Executor, Future, ProcessPoolExecutor, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .classify import classify, extract_fields, project_key
from .extractors import extract, find_oda_converter
from .store import DataPool, FileRecord

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

logger = logging.getLogger("win32-mcp.datapool")

DEFAULT_EXTENSIONS = frozenset(
    {
        ".dwg",
        ".dxf",
        ".pdf",
        ".docx",
        ".pptx",
        ".xlsx",
        ".xlsm",
        ".csv",
        ".txt",
        ".ncn",
        ".kml",
    }
)
SKIP_DIRS = frozenset({".git", "__pycache__", "node_modules", "$recycle.bin", ".tmp.drivedownload", ".tmp.driveupload"})
MAX_FILE_BYTES = 500 * 1024 * 1024

# Windows file attributes set on OneDrive "files on demand" placeholders.
_FILE_ATTRIBUTE_OFFLINE = 0x1000
_FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x40000
_FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x400000
_CLOUD_ONLY_MASK = _FILE_ATTRIBUTE_OFFLINE | _FILE_ATTRIBUTE_RECALL_ON_OPEN | _FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS


def onedrive_roots() -> list[Path]:
    """OneDrive folders advertised by the OneDrive client through environment variables."""
    seen: list[Path] = []
    for var in ("OneDriveCommercial", "OneDriveConsumer", "OneDrive"):
        value = os.getenv(var, "").strip()
        if value and Path(value).is_dir() and Path(value) not in seen:
            seen.append(Path(value))
    return seen


def configured_roots() -> list[Path]:
    """``WIN32_MCP_DATAPOOL_ROOTS`` (``;``-separated) or, when unset, the user's OneDrive folders."""
    env = os.getenv("WIN32_MCP_DATAPOOL_ROOTS", "").strip()
    if env:
        return [Path(p).expanduser().resolve() for p in env.split(";") if p.strip()]
    return [p.resolve() for p in onedrive_roots()]


def is_cloud_only(st: os.stat_result) -> bool:
    """True for OneDrive placeholders whose bytes are not on disk (reading would download them)."""
    attrs = getattr(st, "st_file_attributes", 0)
    return bool(attrs & _CLOUD_ONLY_MASK)


@dataclass
class ScanOptions:
    extensions: frozenset[str] = DEFAULT_EXTENSIONS
    workers: int = max(1, min(4, (os.cpu_count() or 2) - 1))
    hydrate: bool = False
    force: bool = False
    prune: bool = True
    max_files: int = 0
    time_budget_seconds: float = 0.0
    project_depth: int = 1
    use_processes: bool = True
    oda_converter: str | None = None


@dataclass
class ScanReport:
    roots: list[str] = field(default_factory=list)
    seen: int = 0
    indexed: int = 0
    unchanged: int = 0
    cloud_only: int = 0
    errors: int = 0
    pruned: int = 0
    skipped_large: int = 0
    stopped_early: bool = False
    elapsed_seconds: float = 0.0
    oda_converter: str | None = None
    error_samples: list[dict[str, str]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class _Job:
    path: Path
    root: Path
    rel_path: str
    size: int
    mtime: float
    cloud_only: bool


def iter_files(root: Path, extensions: frozenset[str]) -> Iterator[tuple[Path, os.stat_result]]:
    """Yield (path, stat) for matching files without following symlinks/junctions."""
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if entry.name.lower() not in SKIP_DIRS and not entry.name.startswith("~"):
                                stack.append(Path(entry.path))
                            continue
                        if not entry.is_file(follow_symlinks=False):
                            continue
                        name = entry.name
                        if name.startswith(("~$", ".~")):  # Office/AutoCAD lock files
                            continue
                        if Path(name).suffix.lower() not in extensions:
                            continue
                        yield Path(entry.path), entry.stat(follow_symlinks=False)
                    except OSError as exc:
                        logger.warning("Cannot stat %s: %s", entry.path, exc)
        except OSError as exc:
            logger.warning("Cannot list %s: %s", current, exc)


def _process(job: _Job, oda_converter: str | None, project_depth: int) -> FileRecord:
    """Worker body — runs in a separate process, so it only takes/returns picklable data."""
    ext = job.path.suffix.lower()
    if job.cloud_only:
        category, _ = classify(job.rel_path, ext)
        return FileRecord(
            path=str(job.path),
            root=str(job.root),
            rel_path=job.rel_path,
            name=job.path.name,
            ext=ext,
            size=job.size,
            mtime=job.mtime,
            category=category,
            project=project_key(job.rel_path, project_depth),
            doc_type=ext.lstrip("."),
            status="cloud_only",
            error="",
            meta={},
            text="",
        )

    result = extract(job.path, oda_converter=oda_converter)
    category, scores = classify(job.rel_path, ext, result.text)
    meta = dict(result.meta)
    meta["category_scores"] = scores
    fields = extract_fields(result.text, job.rel_path)
    if fields:
        meta["fields"] = fields
    return FileRecord(
        path=str(job.path),
        root=str(job.root),
        rel_path=job.rel_path,
        name=job.path.name,
        ext=ext,
        size=job.size,
        mtime=job.mtime,
        category=category,
        project=project_key(job.rel_path, project_depth),
        doc_type=result.doc_type,
        status="partial" if result.error else "ok",
        error=result.error,
        meta=meta,
        text=result.text,
    )


def scan(
    pool: DataPool,
    roots: Sequence[Path | str],
    options: ScanOptions | None = None,
    progress: Callable[[ScanReport, str], None] | None = None,
) -> ScanReport:
    """Index every matching file under ``roots`` into ``pool``."""
    opts = options or ScanOptions()
    # None = auto-detect, "" = explicitly disabled.
    oda = find_oda_converter() if opts.oda_converter is None else (opts.oda_converter or None)
    report = ScanReport(oda_converter=oda)
    started = time.monotonic()
    submitted = 0

    executor: Executor
    if opts.use_processes and opts.workers > 1:
        executor = ProcessPoolExecutor(max_workers=opts.workers)
    else:
        executor = ThreadPoolExecutor(max_workers=max(1, opts.workers))

    try:
        for raw_root in roots:
            root = Path(raw_root).expanduser().resolve()
            if not root.is_dir():
                raise NotADirectoryError(str(root))
            report.roots.append(str(root))
            seen_paths: set[str] = set()
            pending: list[Future[FileRecord]] = []
            complete = True

            for path, st in iter_files(root, opts.extensions):
                if _out_of_budget(opts, submitted, started):
                    report.stopped_early = True
                    complete = False
                    break
                report.seen += 1
                seen_paths.add(str(path))
                if not stat.S_ISREG(st.st_mode):
                    continue
                if st.st_size > MAX_FILE_BYTES:
                    report.skipped_large += 1
                    continue
                if not opts.force and pool.is_current(str(path), st.st_size, st.st_mtime):
                    report.unchanged += 1
                    continue
                cloud = is_cloud_only(st) and not opts.hydrate
                job = _Job(path, root, path.relative_to(root).as_posix(), st.st_size, st.st_mtime, cloud)
                pending.append(executor.submit(_process, job, oda, opts.project_depth))
                submitted += 1
                if len(pending) >= opts.workers * 4:
                    _drain(pool, pending, report, progress)
                    pending = []

            _drain(pool, pending, report, progress)
            if opts.prune and complete:
                with pool.transaction():
                    report.pruned += pool.prune(str(root), seen_paths)
    finally:
        executor.shutdown(wait=True, cancel_futures=True)

    report.elapsed_seconds = round(time.monotonic() - started, 2)
    return report


def _out_of_budget(opts: ScanOptions, submitted: int, started: float) -> bool:
    if opts.max_files and submitted >= opts.max_files:
        return True
    return bool(opts.time_budget_seconds and time.monotonic() - started >= opts.time_budget_seconds)


def _drain(
    pool: DataPool,
    futures: list[Future[FileRecord]],
    report: ScanReport,
    progress: Callable[[ScanReport, str], None] | None,
) -> None:
    if not futures:
        return
    with pool.transaction():
        for fut in futures:
            try:
                rec = fut.result()
            except Exception as exc:
                report.errors += 1
                if len(report.error_samples) < 20:
                    report.error_samples.append({"error": f"{type(exc).__name__}: {exc}"})
                continue
            pool.upsert(rec)
            if rec.status == "cloud_only":
                report.cloud_only += 1
            else:
                report.indexed += 1
                if rec.error and len(report.error_samples) < 20:
                    report.error_samples.append({"path": rec.rel_path, "error": rec.error})
            if progress:
                progress(report, rec.rel_path)
