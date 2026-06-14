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

    def upsert_file(
        self,
        path: str,
        directory: str,
        filename: str,
        extension: str,
        mtime: float,
        size: int,
    ):
        """Insert or update a file row (identity + freshness stats).

        The row exists so a proposed change can attach to it; freshness is
        tracked via file_mtime/file_size for the apply-time staleness check.
        """
        self.conn.execute(
            """INSERT INTO files (path, directory, filename, extension, file_mtime,
               file_size, applied)
               VALUES (?, ?, ?, ?, ?, ?, 0)
               ON CONFLICT(path) DO UPDATE SET
               directory=excluded.directory,
               filename=excluded.filename,
               extension=excluded.extension,
               file_mtime=excluded.file_mtime,
               file_size=excluded.file_size,
               proposed_json=NULL,
               confidence_time=NULL,
               confidence_gps=NULL,
               applied=0""",
            (path, directory, filename, extension, mtime, size),
        )

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

    def clear_proposals_under_root(self, root: str) -> int:
        """Clear stored proposals for files under `root`.

        Called at the start of a (full) scan so a re-scan fully redefines the
        proposals in its scope — a file that no longer warrants a change loses
        its stale proposal instead of lingering as pending for apply. Uses the
        same trailing-slash prefix rule as get_pending_changes to avoid sibling
        matches (e.g. /foo must not match /foobar).
        """
        root_prefix = root if root.endswith("/") else root + "/"
        rows = self.conn.execute(
            "SELECT path FROM files WHERE proposed_json IS NOT NULL"
        ).fetchall()
        to_clear = [
            r["path"] for r in rows
            if r["path"] == root or r["path"].startswith(root_prefix)
        ]
        for path in to_clear:
            self.conn.execute(
                "UPDATE files SET proposed_json = NULL, confidence_time = NULL, "
                "confidence_gps = NULL WHERE path = ?",
                (path,),
            )
        self.conn.commit()
        return len(to_clear)

    def get_pending_changes(
        self,
        root: Optional[str] = None,
        check_freshness: bool = True,
    ) -> list[dict]:
        """Get all proposed changes that haven't been applied yet.

        Args:
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
