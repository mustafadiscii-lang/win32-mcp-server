"""SQLite storage for the data pool: file records, full-text index, agent reviews."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

    from typing_extensions import Self

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    id           INTEGER PRIMARY KEY,
    path         TEXT NOT NULL UNIQUE,
    root         TEXT NOT NULL,
    rel_path     TEXT NOT NULL,
    name         TEXT NOT NULL,
    ext          TEXT NOT NULL,
    size         INTEGER NOT NULL,
    mtime        REAL NOT NULL,
    category     TEXT NOT NULL,
    project      TEXT NOT NULL,
    doc_type     TEXT NOT NULL,
    status       TEXT NOT NULL,
    error        TEXT NOT NULL DEFAULT '',
    meta_json    TEXT NOT NULL DEFAULT '{}',
    text         TEXT NOT NULL DEFAULT '',
    indexed_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_files_category ON files(category);
CREATE INDEX IF NOT EXISTS idx_files_project ON files(project);
CREATE INDEX IF NOT EXISTS idx_files_root ON files(root);

CREATE TABLE IF NOT EXISTS reviews (
    file_id      INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
    summary      TEXT NOT NULL DEFAULT '',
    tags         TEXT NOT NULL DEFAULT '',
    category     TEXT NOT NULL DEFAULT '',
    fields_json  TEXT NOT NULL DEFAULT '{}',
    reviewed_by  TEXT NOT NULL DEFAULT '',
    reviewed_at  REAL NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS files_fts USING fts5(
    name, rel_path, project, text, summary, tags,
    tokenize = "unicode61 remove_diacritics 2"
);

CREATE TABLE IF NOT EXISTS pool_info (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

_FTS_TOKEN = re.compile(r"\w+", re.UNICODE)


def default_db_path() -> Path:
    """``WIN32_MCP_DATAPOOL_DB`` or ``%LOCALAPPDATA%/win32-mcp/datapool.sqlite``."""
    env = os.getenv("WIN32_MCP_DATAPOOL_DB", "").strip()
    if env:
        return Path(env).expanduser()
    base = os.getenv("LOCALAPPDATA") or str(Path.home() / ".local" / "share")
    return Path(base) / "win32-mcp" / "datapool.sqlite"


@dataclass
class FileRecord:
    path: str
    root: str
    rel_path: str
    name: str
    ext: str
    size: int
    mtime: float
    category: str
    project: str
    doc_type: str
    status: str
    error: str
    meta: dict[str, Any]
    text: str


class DataPool:
    """Thin wrapper around the SQLite pool database."""

    def __init__(self, db_path: Path | str | None = None) -> None:
        self.db_path = Path(db_path) if db_path else default_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path), timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(_SCHEMA)
        self.conn.execute(
            "INSERT OR IGNORE INTO pool_info(key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def is_current(self, path: str, size: int, mtime: float) -> bool:
        """True when the stored record matches size/mtime and was fully indexed."""
        row = self.conn.execute("SELECT size, mtime, status FROM files WHERE path = ?", (path,)).fetchone()
        return bool(row and row["size"] == size and abs(row["mtime"] - mtime) < 1e-3 and row["status"] == "ok")

    def upsert(self, rec: FileRecord) -> int:
        cur = self.conn.execute(
            """
            INSERT INTO files (path, root, rel_path, name, ext, size, mtime, category, project,
                               doc_type, status, error, meta_json, text, indexed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                root=excluded.root, rel_path=excluded.rel_path, name=excluded.name, ext=excluded.ext,
                size=excluded.size, mtime=excluded.mtime, category=excluded.category,
                project=excluded.project, doc_type=excluded.doc_type, status=excluded.status,
                error=excluded.error, meta_json=excluded.meta_json, text=excluded.text,
                indexed_at=excluded.indexed_at
            RETURNING id
            """,
            (
                rec.path,
                rec.root,
                rec.rel_path,
                rec.name,
                rec.ext,
                rec.size,
                rec.mtime,
                rec.category,
                rec.project,
                rec.doc_type,
                rec.status,
                rec.error,
                json.dumps(rec.meta, ensure_ascii=False, default=str),
                rec.text,
                time.time(),
            ),
        )
        file_id = int(cur.fetchone()[0])
        self._refresh_fts(file_id)
        return file_id

    def annotate(
        self,
        file_id: int,
        *,
        summary: str = "",
        tags: Iterable[str] = (),
        category: str = "",
        fields: dict[str, Any] | None = None,
        reviewed_by: str = "agent",
    ) -> None:
        """Store (or replace) an agent's review of a file."""
        if not self.conn.execute("SELECT 1 FROM files WHERE id = ?", (file_id,)).fetchone():
            raise KeyError(file_id)
        tag_text = ", ".join(sorted({t.strip() for t in tags if t.strip()}))
        self.conn.execute(
            """
            INSERT INTO reviews (file_id, summary, tags, category, fields_json, reviewed_by, reviewed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(file_id) DO UPDATE SET
                summary=excluded.summary, tags=excluded.tags, category=excluded.category,
                fields_json=excluded.fields_json, reviewed_by=excluded.reviewed_by,
                reviewed_at=excluded.reviewed_at
            """,
            (
                file_id,
                summary,
                tag_text,
                category,
                json.dumps(fields or {}, ensure_ascii=False, default=str),
                reviewed_by,
                time.time(),
            ),
        )
        self._refresh_fts(file_id)

    def prune(self, root: str, seen_paths: set[str]) -> int:
        """Delete records under ``root`` whose files were not seen in the latest scan."""
        rows = self.conn.execute("SELECT id, path FROM files WHERE root = ?", (root,)).fetchall()
        stale = [row["id"] for row in rows if row["path"] not in seen_paths]
        for file_id in stale:
            self.conn.execute("DELETE FROM files_fts WHERE rowid = ?", (file_id,))
            self.conn.execute("DELETE FROM files WHERE id = ?", (file_id,))
        return len(stale)

    def _refresh_fts(self, file_id: int) -> None:
        row = self.conn.execute(
            """
            SELECT f.name, f.rel_path, f.project, f.text, COALESCE(r.summary, '') AS summary,
                   COALESCE(r.tags, '') AS tags
            FROM files f LEFT JOIN reviews r ON r.file_id = f.id WHERE f.id = ?
            """,
            (file_id,),
        ).fetchone()
        self.conn.execute("DELETE FROM files_fts WHERE rowid = ?", (file_id,))
        if row:
            self.conn.execute(
                "INSERT INTO files_fts(rowid, name, rel_path, project, text, summary, tags) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (file_id, row["name"], row["rel_path"], row["project"], row["text"], row["summary"], row["tags"]),
            )

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def search(
        self,
        query: str = "",
        *,
        category: str = "",
        project: str = "",
        ext: str = "",
        limit: int = 20,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        where: list[str] = []
        params: list[Any] = []
        if category:
            where.append("COALESCE(NULLIF(r.category, ''), f.category) = ?")
            params.append(category)
        if project:
            where.append("f.project LIKE ?")
            params.append(f"%{project}%")
        if ext:
            where.append("f.ext = ?")
            params.append(ext if ext.startswith(".") else f".{ext}")

        match = to_fts_query(query)
        if match:
            sql = (
                "SELECT f.*, r.summary, r.tags, r.category AS review_category, "
                "snippet(files_fts, 3, '[', ']', ' … ', 12) AS snippet, bm25(files_fts) AS rank "
                "FROM files_fts JOIN files f ON f.id = files_fts.rowid "
                "LEFT JOIN reviews r ON r.file_id = f.id WHERE files_fts MATCH ?"
            )
            params.insert(0, match)
            if where:
                sql += " AND " + " AND ".join(where)
            sql += " ORDER BY rank LIMIT ? OFFSET ?"
        else:
            sql = (
                "SELECT f.*, r.summary, r.tags, r.category AS review_category, '' AS snippet "
                "FROM files f LEFT JOIN reviews r ON r.file_id = f.id"
            )
            if where:
                sql += " WHERE " + " AND ".join(where)
            sql += " ORDER BY f.project, f.rel_path LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        return [_summary_row(row) for row in self.conn.execute(sql, params)]

    def get(self, file_id: int, *, max_text_chars: int = 8000) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT f.*, r.summary, r.tags, r.category AS review_category, r.fields_json, r.reviewed_by, "
            "r.reviewed_at FROM files f LEFT JOIN reviews r ON r.file_id = f.id WHERE f.id = ?",
            (file_id,),
        ).fetchone()
        if not row:
            return None
        out = _summary_row(row)
        text = row["text"] or ""
        out["meta"] = json.loads(row["meta_json"] or "{}")
        out["error"] = row["error"]
        out["text_chars"] = len(text)
        out["text"] = text[:max_text_chars]
        out["text_truncated"] = len(text) > max_text_chars
        out["review_fields"] = json.loads(row["fields_json"] or "{}")
        out["reviewed_by"] = row["reviewed_by"] or ""
        return out

    def pending_reviews(self, *, category: str = "", project: str = "", limit: int = 10) -> list[dict[str, Any]]:
        """Files no agent has reviewed yet, grouped by project."""
        sql = (
            "SELECT f.*, NULL AS summary, NULL AS tags, NULL AS review_category, '' AS snippet "
            "FROM files f LEFT JOIN reviews r ON r.file_id = f.id WHERE r.file_id IS NULL"
        )
        params: list[Any] = []
        if category:
            sql += " AND f.category = ?"
            params.append(category)
        if project:
            sql += " AND f.project LIKE ?"
            params.append(f"%{project}%")
        sql += " ORDER BY f.project, f.rel_path LIMIT ?"
        params.append(limit)
        return [_summary_row(row) for row in self.conn.execute(sql, params)]

    def stats(self) -> dict[str, Any]:
        def grouped(sql: str) -> dict[str, int]:
            return {str(row[0]): int(row[1]) for row in self.conn.execute(sql)}

        total = self.conn.execute("SELECT COUNT(*), COALESCE(SUM(size), 0) FROM files").fetchone()
        reviewed = self.conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0]
        return {
            "db_path": str(self.db_path),
            "files": int(total[0]),
            "total_bytes": int(total[1]),
            "reviewed": int(reviewed),
            "by_category": grouped(
                "SELECT COALESCE(NULLIF(r.category, ''), f.category), COUNT(*) FROM files f "
                "LEFT JOIN reviews r ON r.file_id = f.id GROUP BY 1 ORDER BY 2 DESC"
            ),
            "by_ext": grouped("SELECT ext, COUNT(*) FROM files GROUP BY ext ORDER BY 2 DESC"),
            "by_status": grouped("SELECT status, COUNT(*) FROM files GROUP BY status ORDER BY 2 DESC"),
            "roots": grouped("SELECT root, COUNT(*) FROM files GROUP BY root ORDER BY 2 DESC"),
        }

    def projects(self, limit: int = 200) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT f.project,
                   COUNT(*) AS files,
                   SUM(f.ext IN ('.dwg', '.dxf')) AS drawings,
                   SUM(f.category = 'etut_plani') AS etut_plani,
                   SUM(f.category = 'teklif_sunumu') AS teklif_sunumu,
                   SUM(f.category = 'veri') AS veri,
                   COUNT(r.file_id) AS reviewed,
                   MAX(f.mtime) AS last_modified
            FROM files f LEFT JOIN reviews r ON r.file_id = f.id
            GROUP BY f.project ORDER BY last_modified DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    def export_rows(self) -> Iterator[dict[str, Any]]:
        """Yield every record (without full text) for JSONL/CSV export."""
        for row in self.conn.execute(
            "SELECT f.*, r.summary, r.tags, r.category AS review_category, r.fields_json "
            "FROM files f LEFT JOIN reviews r ON r.file_id = f.id ORDER BY f.project, f.rel_path"
        ):
            out = _summary_row(row)
            out["meta"] = json.loads(row["meta_json"] or "{}")
            out["review_fields"] = json.loads(row["fields_json"] or "{}")
            yield out


def to_fts_query(query: str) -> str:
    """Turn free text into a safe FTS5 query: every word must match (prefix match on the last)."""
    tokens = _FTS_TOKEN.findall(query)
    if not tokens:
        return ""
    quoted = [f'"{tok}"' for tok in tokens]
    quoted[-1] += "*"
    return " AND ".join(quoted)


def _summary_row(row: sqlite3.Row) -> dict[str, Any]:
    keys = row.keys()
    out: dict[str, Any] = {
        "id": row["id"],
        "path": row["path"],
        "rel_path": row["rel_path"],
        "project": row["project"],
        "category": (row["review_category"] if "review_category" in keys and row["review_category"] else None)
        or row["category"],
        "auto_category": row["category"],
        "doc_type": row["doc_type"],
        "ext": row["ext"],
        "size": row["size"],
        "mtime": row["mtime"],
        "status": row["status"],
    }
    if "summary" in keys and row["summary"]:
        out["summary"] = row["summary"]
    if "tags" in keys and row["tags"]:
        out["tags"] = row["tags"]
    if "snippet" in keys and row["snippet"]:
        out["snippet"] = row["snippet"]
    return out
