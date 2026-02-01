"""SQLite metadata cache — contract between scan and apply phases."""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY,
    directory TEXT NOT NULL,
    filename TEXT NOT NULL,
    extension TEXT NOT NULL,
    file_mtime REAL NOT NULL,
    file_size INTEGER NOT NULL,
    metadata_json TEXT NOT NULL,
    scan_version INTEGER DEFAULT 0,
    proposed_json TEXT,
    confidence_time TEXT,
    confidence_gps TEXT,
    applied INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_directory ON files(directory);
CREATE INDEX IF NOT EXISTS idx_applied ON files(applied);
CREATE INDEX IF NOT EXISTS idx_proposed ON files(proposed_json) WHERE proposed_json IS NOT NULL;

CREATE TABLE IF NOT EXISTS scan_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    root TEXT NOT NULL,
    file_count INTEGER DEFAULT 0,
    changes_proposed INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS dir_flags (
    directory TEXT PRIMARY KEY,
    bulk_copied INTEGER DEFAULT 0,
    scan_version INTEGER DEFAULT 0
);
"""


class MetadataCache:
    """SQLite-backed metadata cache."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self._init_schema()

    def _init_schema(self):
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def is_fresh(self, path: str, mtime: float, size: int) -> bool:
        """Check if cached entry matches current file."""
        row = self.conn.execute(
            "SELECT file_mtime, file_size FROM files WHERE path = ?",
            (path,),
        ).fetchone()
        if row is None:
            return False
        return abs(row["file_mtime"] - mtime) < 0.001 and row["file_size"] == size

    def upsert_file(
        self,
        path: str,
        directory: str,
        filename: str,
        extension: str,
        mtime: float,
        size: int,
        metadata: dict,
        scan_version: int = 0,
    ):
        """Insert or update a file's metadata."""
        self.conn.execute(
            """INSERT INTO files (path, directory, filename, extension, file_mtime,
               file_size, metadata_json, scan_version, applied)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
               ON CONFLICT(path) DO UPDATE SET
               directory=excluded.directory,
               filename=excluded.filename,
               extension=excluded.extension,
               file_mtime=excluded.file_mtime,
               file_size=excluded.file_size,
               metadata_json=excluded.metadata_json,
               scan_version=excluded.scan_version,
               proposed_json=NULL,
               confidence_time=NULL,
               confidence_gps=NULL,
               applied=0""",
            (path, directory, filename, extension, mtime, size,
             json.dumps(metadata), scan_version),
        )

    def get_directory_files(self, directory: str) -> list[dict]:
        """Get all cached file records for a directory."""
        rows = self.conn.execute(
            "SELECT path, metadata_json FROM files WHERE directory = ?",
            (directory,),
        ).fetchall()
        result = []
        for row in rows:
            try:
                metadata = json.loads(row["metadata_json"])
                metadata["_cached_path"] = row["path"]
                result.append(metadata)
            except json.JSONDecodeError:
                logger.warning("Corrupt cache entry for %s", row["path"])
        return result

    def set_proposed_change(
        self,
        path: str,
        proposed: dict,
        confidence_time: Optional[str] = None,
        confidence_gps: Optional[str] = None,
    ):
        """Store a proposed change for a file."""
        self.conn.execute(
            """UPDATE files SET proposed_json = ?, confidence_time = ?,
               confidence_gps = ? WHERE path = ?""",
            (json.dumps(proposed), confidence_time, confidence_gps, path),
        )

    def get_pending_changes(
        self,
        min_confidence_time: Optional[str] = None,
        min_confidence_gps: Optional[str] = None,
        root: Optional[str] = None,
        check_freshness: bool = True,
    ) -> list[dict]:
        """Get all proposed changes that haven't been applied yet.

        Args:
            min_confidence_time: Minimum time confidence (unused, kept for API compat).
            min_confidence_gps: Minimum GPS confidence (unused, kept for API compat).
            root: If provided, only return changes under this root directory.
            check_freshness: If True, skip changes where file has been modified since scan.

        Returns list of proposed changes with freshness and root filtering applied.
        """
        rows = self.conn.execute(
            """SELECT path, proposed_json, confidence_time, confidence_gps,
                      file_mtime, file_size
               FROM files
               WHERE proposed_json IS NOT NULL AND applied = 0""",
        ).fetchall()
        result = []
        for row in rows:
            path = row["path"]

            # Root filtering: ensure prefix ends with / to avoid matching siblings
            # (e.g. "/photos/Albums" must not match "/photos/Albums2/file.jpg")
            if root:
                root_prefix = root if root.endswith("/") else root + "/"
                if not (path.startswith(root_prefix) or path == root):
                    continue

            # Freshness check: compare current file stats against cached values
            if check_freshness:
                from pathlib import Path as _Path
                p = _Path(path)
                if not p.exists():
                    logger.warning(
                        "Skipping proposal for missing file %s", path
                    )
                    continue
                try:
                    st = p.stat()
                    current_mtime = st.st_mtime
                    current_size = st.st_size
                    cached_mtime = row["file_mtime"]
                    cached_size = row["file_size"]
                    if abs(current_mtime - cached_mtime) >= 0.001 or current_size != cached_size:
                        logger.warning(
                            "Skipping stale proposal for %s (file modified since scan)", path
                        )
                        continue
                except OSError:
                    logger.warning(
                        "Skipping proposal for inaccessible file %s", path
                    )
                    continue

            try:
                proposed = json.loads(row["proposed_json"])
                proposed["_db_path"] = row["path"]
                proposed["_confidence_time"] = row["confidence_time"]
                proposed["_confidence_gps"] = row["confidence_gps"]
                result.append(proposed)
            except json.JSONDecodeError:
                logger.warning("Corrupt proposed change for %s", row["path"])
        return result

    def mark_applied(self, path: str):
        """Mark a file as having had its changes applied."""
        self.conn.execute(
            "UPDATE files SET applied = 1 WHERE path = ?",
            (path,),
        )

    def set_dir_flag(self, directory: str, bulk_copied: bool, scan_version: int = 0):
        """Set directory-level flags."""
        self.conn.execute(
            """INSERT INTO dir_flags (directory, bulk_copied, scan_version)
               VALUES (?, ?, ?)
               ON CONFLICT(directory) DO UPDATE SET
               bulk_copied=excluded.bulk_copied,
               scan_version=excluded.scan_version""",
            (directory, int(bulk_copied), scan_version),
        )

    def is_dir_bulk_copied(self, directory: str) -> Optional[bool]:
        """Check if a directory was flagged as bulk-copied."""
        row = self.conn.execute(
            "SELECT bulk_copied FROM dir_flags WHERE directory = ?",
            (directory,),
        ).fetchone()
        if row is None:
            return None
        return bool(row["bulk_copied"])

    def start_scan_run(self, root: str) -> int:
        """Record the start of a scan run. Returns run_id."""
        cursor = self.conn.execute(
            "INSERT INTO scan_runs (started_at, root) VALUES (?, ?)",
            (datetime.now().isoformat(), root),
        )
        self.conn.commit()
        return cursor.lastrowid

    def finish_scan_run(self, run_id: int, file_count: int, changes: int):
        """Record the end of a scan run."""
        self.conn.execute(
            """UPDATE scan_runs SET finished_at = ?, file_count = ?,
               changes_proposed = ? WHERE run_id = ?""",
            (datetime.now().isoformat(), file_count, changes, run_id),
        )
        self.conn.commit()

    def commit(self):
        """Commit pending changes."""
        self.conn.commit()

    def get_stats(self) -> dict:
        """Get cache statistics."""
        total = self.conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        proposed = self.conn.execute(
            "SELECT COUNT(*) FROM files WHERE proposed_json IS NOT NULL"
        ).fetchone()[0]
        applied = self.conn.execute(
            "SELECT COUNT(*) FROM files WHERE applied = 1"
        ).fetchone()[0]
        return {
            "total_files": total,
            "proposed_changes": proposed,
            "applied_changes": applied,
        }
