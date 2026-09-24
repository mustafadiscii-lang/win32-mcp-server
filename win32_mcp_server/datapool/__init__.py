"""
Data pool — local indexer for engineering document archives (OneDrive, shared drives).

Walks one or more root folders, extracts searchable content from DWG/DXF drawings,
PDFs, Office documents and plain-text data files, classifies each file
(proje / etut_plani / teklif_sunumu / veri) and stores everything in a SQLite
database with a full-text index. Local AI agents then query the pool and write
their own reviews back into it through the ``datapool_*`` MCP tools.

This subpackage has no Win32 dependencies so it can be tested on any platform.
"""

from .store import DataPool, default_db_path

__all__ = ["DataPool", "default_db_path"]
