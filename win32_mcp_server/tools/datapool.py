"""
Data pool tools — let local agents build and query an index of project archives.

Tools:
  - datapool_index            Scan allowed roots (OneDrive by default) into the pool
  - datapool_stats            Totals by category / extension / status
  - datapool_projects         Per-project summary (drawings, etüt, teklif, veri counts)
  - datapool_search           Full-text search with category/project/extension filters
  - datapool_get              One record: metadata, extracted text, agent review
  - datapool_pending_reviews  Files no agent has reviewed yet
  - datapool_annotate         Store an agent's summary/tags/category for a file

Indexing only reads files under the allowed roots: ``WIN32_MCP_DATAPOOL_ROOTS``
(``;``-separated) or, when unset, the OneDrive folders of the current user.
"""

import asyncio
import threading
from pathlib import Path
from typing import Any

from ..datapool.classify import CATEGORIES
from ..datapool.scanner import ScanOptions, configured_roots, scan
from ..datapool.store import DataPool
from ..registry import registry
from ..utils.args import get_bool, get_int, get_str
from ..utils.errors import ToolError

MAX_INDEX_SECONDS = 150
_INDEX_LOCK = threading.Lock()


def allowed_roots() -> list[Path]:
    return configured_roots()


def _resolve_roots(requested: list[str]) -> list[Path]:
    allowed = allowed_roots()
    if not allowed:
        raise ToolError(
            "No data pool roots configured and no OneDrive folder found",
            suggestion="Set WIN32_MCP_DATAPOOL_ROOTS, e.g. C:\\Users\\me\\OneDrive\\Projeler",
        )
    if not requested:
        return allowed
    resolved = []
    for raw in requested:
        path = Path(raw).expanduser().resolve()
        if not any(path == root or root in path.parents for root in allowed):
            raise ToolError(
                f"Path is outside the allowed data pool roots: {raw}",
                suggestion=f"Allowed roots: {', '.join(str(r) for r in allowed)}",
            )
        resolved.append(path)
    return resolved


def _category_arg(arguments: dict[str, Any]) -> str:
    category = get_str(arguments, "category", default="", max_length=32)
    if category and category not in CATEGORIES:
        raise ToolError(f"Unknown category '{category}'", suggestion=f"Use one of: {', '.join(CATEGORIES)}")
    return category


_CATEGORY_SCHEMA = {"type": "string", "enum": ["", *CATEGORIES], "description": "Filter by category"}


@registry.register(
    "datapool_index",
    "Scan project archive folders (OneDrive by default) and index DWG/DXF drawings, PDFs, Office files and "
    "data files into the local data pool. Incremental: unchanged files are skipped. Call repeatedly while "
    "'stopped_early' is true.",
    {
        "type": "object",
        "properties": {
            "roots": {
                "type": "array",
                "items": {"type": "string", "maxLength": 1000},
                "maxItems": 20,
                "description": "Folders to scan (must be inside the allowed roots; default: all allowed roots)",
            },
            "max_files": {"type": "integer", "minimum": 1, "maximum": 5000, "description": "Files per call"},
            "time_budget_seconds": {"type": "integer", "minimum": 5, "maximum": MAX_INDEX_SECONDS},
            "workers": {"type": "integer", "minimum": 1, "maximum": 16, "description": "Parallel workers"},
            "hydrate": {"type": "boolean", "description": "Download cloud-only OneDrive files to read them"},
            "force": {
                "type": "boolean",
                "description": "Re-extract files even if unchanged (repeats every call; do not loop on stopped_early)",
            },
            "project_depth": {
                "type": "integer",
                "minimum": 1,
                "maximum": 5,
                "description": "Folder levels under the allowed root that name a project (default 1)",
            },
        },
    },
)
async def handle_datapool_index(arguments: dict[str, Any]) -> dict[str, Any]:
    raw_roots = arguments.get("roots") or []
    roots = _resolve_roots([str(r) for r in raw_roots])
    opts = ScanOptions(
        max_files=get_int(arguments, "max_files", default=300, min_value=1, max_value=5000),
        time_budget_seconds=get_int(
            arguments, "time_budget_seconds", default=120, min_value=5, max_value=MAX_INDEX_SECONDS
        ),
        workers=get_int(arguments, "workers", default=ScanOptions().workers, min_value=1, max_value=16),
        hydrate=get_bool(arguments, "hydrate", default=False),
        force=get_bool(arguments, "force", default=False),
        project_depth=get_int(arguments, "project_depth", default=1, min_value=1, max_value=5),
        base_roots=tuple(allowed_roots()),
        # Threads keep child processes away from the MCP stdio pipes.
        use_processes=False,
    )

    def _run() -> dict[str, Any]:
        # A timed-out call leaves its worker thread running; refuse to start a second, overlapping
        # scan (they would prune each other's fresh records) until that one has finished.
        if not _INDEX_LOCK.acquire(blocking=False):
            raise ToolError(
                "A data pool scan is already running",
                suggestion="Wait for it to finish, then call datapool_index again",
            )
        try:
            with DataPool() as pool:
                return scan(pool, roots, opts).as_dict()
        finally:
            _INDEX_LOCK.release()

    return await asyncio.to_thread(_run)


@registry.register(
    "datapool_stats",
    "Data pool totals: file count, reviewed count, breakdown by category, extension and status",
    {"type": "object", "properties": {}},
)
async def handle_datapool_stats(arguments: dict[str, Any]) -> dict[str, Any]:
    def _run() -> dict[str, Any]:
        with DataPool() as pool:
            stats = pool.stats()
        stats["allowed_roots"] = [str(r) for r in allowed_roots()]
        return stats

    return await asyncio.to_thread(_run)


@registry.register(
    "datapool_projects",
    "Per-project summary of the data pool: file, drawing, etüt planı, teklif/sunum and veri counts",
    {
        "type": "object",
        "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 1000}},
    },
)
async def handle_datapool_projects(arguments: dict[str, Any]) -> dict[str, Any]:
    limit = get_int(arguments, "limit", default=200, min_value=1, max_value=1000)

    def _run() -> dict[str, Any]:
        with DataPool() as pool:
            return {"projects": pool.projects(limit)}

    return await asyncio.to_thread(_run)


@registry.register(
    "datapool_search",
    "Full-text search in the data pool (file names, paths, drawing texts, title blocks, document text, agent "
    "summaries). Turkish diacritics are ignored (etut = etüt).",
    {
        "type": "object",
        "properties": {
            "query": {"type": "string", "maxLength": 500, "description": "Words to find (all must match)"},
            "category": _CATEGORY_SCHEMA,
            "project": {"type": "string", "maxLength": 300, "description": "Project name contains"},
            "ext": {"type": "string", "maxLength": 10, "description": "Extension, e.g. dwg"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            "offset": {"type": "integer", "minimum": 0, "maximum": 100000},
        },
    },
)
async def handle_datapool_search(arguments: dict[str, Any]) -> dict[str, Any]:
    query = get_str(arguments, "query", default="", max_length=500)
    category = _category_arg(arguments)
    project = get_str(arguments, "project", default="", max_length=300)
    ext = get_str(arguments, "ext", default="", max_length=10).lower()
    limit = get_int(arguments, "limit", default=20, min_value=1, max_value=100)
    offset = get_int(arguments, "offset", default=0, min_value=0, max_value=100000)

    def _run() -> dict[str, Any]:
        with DataPool() as pool:
            results = pool.search(query, category=category, project=project, ext=ext, limit=limit, offset=offset)
        return {"count": len(results), "results": results}

    return await asyncio.to_thread(_run)


@registry.register(
    "datapool_get",
    "Full data pool record for one file: metadata (DWG layers, blocks, title block, units, PDF pages...), "
    "extracted text and any agent review",
    {
        "type": "object",
        "properties": {
            "id": {"type": "integer", "minimum": 1},
            "max_text_chars": {"type": "integer", "minimum": 0, "maximum": 50000},
        },
        "required": ["id"],
    },
)
async def handle_datapool_get(arguments: dict[str, Any]) -> dict[str, Any]:
    file_id = get_int(arguments, "id", required=True, min_value=1)
    max_chars = get_int(arguments, "max_text_chars", default=8000, min_value=0, max_value=50000)

    def _run() -> dict[str, Any] | None:
        with DataPool() as pool:
            return pool.get(file_id, max_text_chars=max_chars)

    record = await asyncio.to_thread(_run)
    if record is None:
        raise ToolError(f"No data pool record with id {file_id}", suggestion="Use datapool_search to find ids.")
    return record


@registry.register(
    "datapool_pending_reviews",
    "List indexed files that no agent has reviewed yet — the work queue for reviewing agents",
    {
        "type": "object",
        "properties": {
            "category": _CATEGORY_SCHEMA,
            "project": {"type": "string", "maxLength": 300},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
        },
    },
)
async def handle_datapool_pending_reviews(arguments: dict[str, Any]) -> dict[str, Any]:
    category = _category_arg(arguments)
    project = get_str(arguments, "project", default="", max_length=300)
    limit = get_int(arguments, "limit", default=10, min_value=1, max_value=100)

    def _run() -> dict[str, Any]:
        with DataPool() as pool:
            return {"pending": pool.pending_reviews(category=category, project=project, limit=limit)}

    return await asyncio.to_thread(_run)


@registry.register(
    "datapool_annotate",
    "Save an agent's review of a data pool file: short summary, tags, corrected category and structured "
    "fields (e.g. ada/parsel, ölçek, müşteri, teklif tutarı). Replaces any earlier review of the file.",
    {
        "type": "object",
        "properties": {
            "id": {"type": "integer", "minimum": 1},
            "summary": {"type": "string", "maxLength": 4000},
            "tags": {"type": "array", "items": {"type": "string", "maxLength": 60}, "maxItems": 30},
            "category": _CATEGORY_SCHEMA,
            "fields": {"type": "object", "description": "Structured facts extracted by the agent"},
            "reviewed_by": {"type": "string", "maxLength": 100, "description": "Agent name"},
        },
        "required": ["id"],
    },
)
async def handle_datapool_annotate(arguments: dict[str, Any]) -> dict[str, Any]:
    file_id = get_int(arguments, "id", required=True, min_value=1)
    summary = get_str(arguments, "summary", default="", max_length=4000)
    tags = [str(t) for t in arguments.get("tags") or []]
    category = _category_arg(arguments)
    fields = arguments.get("fields") or {}
    reviewed_by = get_str(arguments, "reviewed_by", default="agent", max_length=100)

    def _run() -> None:
        with DataPool() as pool, pool.transaction():
            pool.annotate(
                file_id, summary=summary, tags=tags, category=category, fields=fields, reviewed_by=reviewed_by
            )

    try:
        await asyncio.to_thread(_run)
    except KeyError as exc:
        raise ToolError(f"No data pool record with id {file_id}") from exc
    return {"saved": True, "id": file_id}
