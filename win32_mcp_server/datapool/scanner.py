"""Folder walker that feeds files through extractors into the pool, in parallel workers."""

from __future__ import annotations

import logging
import os
import stat
import time
from concurrent.futures import BrokenExecutor, Executor, Future, ProcessPoolExecutor, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .classify import classify, extract_fields, project_key
from .extractors import NO_ODA_ERROR, extract, find_oda_converter
from .store import DataPool, FileRecord, StoredState

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
# Partial extractions caused by something other than a missing converter (locked file, ODA timeout,
# offline OneDrive file...) may be temporary; retry them after this long.
PARTIAL_RETRY_SECONDS = 24 * 3600

# Windows file attributes set on OneDrive "files on demand" placeholders.
_FILE_ATTRIBUTE_OFFLINE = 0x1000
_FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x40000
_FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x400000
_CLOUD_ONLY_MASK = _FILE_ATTRIBUTE_OFFLINE | _FILE_ATTRIBUTE_RECALL_ON_OPEN | _FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
# Reparse tags that redirect a directory elsewhere (junction / directory symlink). OneDrive's own
# cloud reparse tags are deliberately not listed: OneDrive folders must still be walked.
_IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003
_IO_REPARSE_TAG_SYMLINK = 0xA000000C


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
    # Allowed roots: rel_path/project are computed against the deepest one containing each file,
    # so scanning a sub-folder gives the same records as scanning the whole root.
    base_roots: tuple[Path, ...] = ()


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
    unreadable_dirs: int = 0
    missing_roots: list[str] = field(default_factory=list)
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


def _is_redirect(entry: os.DirEntry[str]) -> bool:
    """True for symlinks and Windows junctions, which ``is_dir(follow_symlinks=False)`` still reports as dirs."""
    if entry.is_symlink():
        return True
    is_junction = getattr(entry, "is_junction", None)  # Python 3.12+
    if is_junction is not None and is_junction():
        return True
    tag = getattr(entry.stat(follow_symlinks=False), "st_reparse_tag", 0)
    return tag in (_IO_REPARSE_TAG_MOUNT_POINT, _IO_REPARSE_TAG_SYMLINK)


def iter_files(
    root: Path, extensions: frozenset[str], errors: list[str] | None = None
) -> Iterator[tuple[Path, os.stat_result]]:
    """Yield (path, stat) for matching files without following symlinks/junctions.

    Folders that cannot be listed are appended to ``errors`` so callers know the walk was incomplete.
    """
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if _is_redirect(entry):
                                continue
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
                        if errors is not None:
                            # Might be a folder we could not inspect; treat the walk as incomplete.
                            errors.append(entry.path)
        except OSError as exc:
            logger.warning("Cannot list %s: %s", current, exc)
            if errors is not None:
                errors.append(str(current))


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

    executor: Executor
    if opts.use_processes and opts.workers > 1:
        executor = ProcessPoolExecutor(max_workers=opts.workers)
    else:
        executor = ThreadPoolExecutor(max_workers=max(1, opts.workers))

    run = _Run(pool, opts, oda, executor, report, progress, started=time.monotonic())
    bases = [Path(b).expanduser().resolve() for b in opts.base_roots]
    try:
        for raw_root in roots:
            root = Path(raw_root).expanduser().resolve()
            if not root.is_dir():
                # A missing or offline folder must not abort the other roots.
                logger.warning("Data pool root is not a folder: %s", root)
                report.missing_roots.append(str(root))
                continue
            report.roots.append(str(root))
            _scan_root(run, root, bases)
            if report.stopped_early:
                break
    finally:
        executor.shutdown(wait=True, cancel_futures=True)

    report.elapsed_seconds = round(time.monotonic() - run.started, 2)
    return report


@dataclass
class _Run:
    pool: DataPool
    opts: ScanOptions
    oda: str | None
    executor: Executor
    report: ScanReport
    progress: Callable[[ScanReport, str], None] | None
    started: float
    submitted: int = 0


def _scan_root(run: _Run, root: Path, bases: Sequence[Path]) -> None:
    """Walk one root, extract changed files and prune records of files that disappeared."""
    opts, report = run.opts, run.report
    seen_paths: set[str] = set()
    pending: list[Future[FileRecord]] = []
    complete = True
    listing_errors: list[str] = []

    for path, st in iter_files(root, opts.extensions, listing_errors):
        if _out_of_budget(opts, run.submitted, run.started):
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
        base = _base_for(path, bases, root)
        cloud = is_cloud_only(st) and not opts.hydrate
        state = None if opts.force else run.pool.stored_state(str(path), st.st_size, st.st_mtime)
        if _is_current(state, cloud, str(base), run.oda):
            report.unchanged += 1
            continue
        job = _Job(path, base, path.relative_to(base).as_posix(), st.st_size, st.st_mtime, cloud)
        try:
            pending.append(run.executor.submit(_process, job, run.oda, opts.project_depth))
        except BrokenExecutor as exc:
            # A crashed worker process poisons the pool; stop cleanly and keep what was indexed.
            logger.warning("Worker pool broke, stopping scan: %s", exc)
            report.errors += 1
            report.error_samples.append({"error": f"{type(exc).__name__}: {exc}"})
            report.stopped_early = True
            complete = False
            break
        run.submitted += 1
        if len(pending) >= opts.workers * 4:
            _drain(run.pool, pending, report, run.progress)
            pending = []

    _drain(run.pool, pending, report, run.progress)
    if listing_errors:
        # Files under an unreadable folder were not seen; pruning would delete them and their reviews.
        report.unreadable_dirs += len(listing_errors)
        complete = False
    if opts.prune and complete:
        with run.pool.transaction():
            report.pruned += run.pool.prune(str(root), seen_paths, opts.extensions)


def _base_for(path: Path, bases: Sequence[Path], fallback: Path) -> Path:
    """The deepest allowed root containing ``path``, so a file gets the same rel_path/project
    whichever folder was scanned; ``fallback`` (the scanned folder) when no allowed root contains it."""
    containing = [b for b in bases if b in path.parents]
    return max(containing, key=lambda b: len(b.parts)) if containing else fallback


def _is_current(state: StoredState | None, cloud_only_now: bool, root: str, oda: str | None) -> bool:
    """Whether a stored record with matching size/mtime can be skipped.

    "partial" and "cloud_only" records are normally skipped too, otherwise every call would re-process
    the same files and ``max_files`` would never let the scan move past them. They are retried only
    when retrying can give a different result.
    """
    if state is None:
        return False
    if state.root != root:
        return False  # indexed under another root (or by an older version): refresh rel_path/project
    if state.status == "partial":
        if state.error == NO_ODA_ERROR:
            return not oda  # ODA File Converter has been installed since
        return time.time() - state.indexed_at < PARTIAL_RETRY_SECONDS
    if state.status == "cloud_only":
        return cloud_only_now  # re-read once the file is available locally (or hydrate is on)
    return True


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
    # Wait for every worker before opening the write transaction, so other connections (e.g. an
    # agent annotating a file) are not blocked while slow extractions run.
    records: list[FileRecord] = []
    for fut in futures:
        try:
            records.append(fut.result())
        except Exception as exc:
            report.errors += 1
            if len(report.error_samples) < 20:
                report.error_samples.append({"error": f"{type(exc).__name__}: {exc}"})
    with pool.transaction():
        for rec in records:
            pool.upsert(rec)
            if rec.status == "cloud_only":
                report.cloud_only += 1
            else:
                report.indexed += 1
                if rec.error and len(report.error_samples) < 20:
                    report.error_samples.append({"path": rec.rel_path, "error": rec.error})
            if progress:
                progress(report, rec.rel_path)
