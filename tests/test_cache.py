"""Tests for SQLite metadata cache."""

import json
from pathlib import Path

import pytest

from exif_heal.cache import MetadataCache


class TestMetadataCache:

    def test_create_and_schema(self, tmp_dir):
        db_path = tmp_dir / "test.db"
        cache = MetadataCache(db_path)
        assert db_path.exists()
        cache.close()

    def test_upsert_and_query(self, cache_db):
        cache_db.upsert_file(
            path="/test/photo.jpg",
            directory="/test",
            filename="photo.jpg",
            extension="jpg",
            mtime=1609459200.0,
            size=5000,
            metadata={"DateTimeOriginal": "2021:01:01 10:00:00"},
        )
        cache_db.commit()

        rows = cache_db.get_directory_files("/test")
        assert len(rows) == 1
        assert rows[0]["DateTimeOriginal"] == "2021:01:01 10:00:00"

    def test_cache_freshness(self, cache_db):
        cache_db.upsert_file(
            path="/test/photo.jpg",
            directory="/test",
            filename="photo.jpg",
            extension="jpg",
            mtime=1609459200.0,
            size=5000,
            metadata={},
        )
        cache_db.commit()

        # Same mtime and size -> fresh
        assert cache_db.is_fresh("/test/photo.jpg", 1609459200.0, 5000) is True
        # Different mtime -> stale
        assert cache_db.is_fresh("/test/photo.jpg", 1609459201.0, 5000) is False
        # Different size -> stale
        assert cache_db.is_fresh("/test/photo.jpg", 1609459200.0, 6000) is False
        # Unknown file -> stale
        assert cache_db.is_fresh("/test/other.jpg", 1609459200.0, 5000) is False

    def test_proposed_change_roundtrip(self, cache_db):
        cache_db.upsert_file(
            path="/test/photo.jpg",
            directory="/test",
            filename="photo.jpg",
            extension="jpg",
            mtime=1609459200.0,
            size=5000,
            metadata={},
        )

        proposed = {
            "path": "/test/photo.jpg",
            "time": {"datetime_original": "2021:01:01 10:00:00"},
            "gps": {"lat": -34.5, "lon": 138.5},
        }
        cache_db.set_proposed_change(
            "/test/photo.jpg", proposed, "high", "med",
        )
        cache_db.commit()

        pending = cache_db.get_pending_changes(check_freshness=False)
        assert len(pending) == 1
        assert pending[0]["path"] == "/test/photo.jpg"
        assert pending[0]["time"]["datetime_original"] == "2021:01:01 10:00:00"
        assert pending[0]["gps"]["lat"] == -34.5

    def test_mark_applied(self, cache_db):
        cache_db.upsert_file(
            path="/test/photo.jpg",
            directory="/test",
            filename="photo.jpg",
            extension="jpg",
            mtime=1609459200.0,
            size=5000,
            metadata={},
        )
        cache_db.set_proposed_change(
            "/test/photo.jpg", {"path": "/test/photo.jpg"}, "high", None,
        )
        cache_db.commit()

        # Before applying
        pending = cache_db.get_pending_changes(check_freshness=False)
        assert len(pending) == 1

        # After applying
        cache_db.mark_applied("/test/photo.jpg")
        cache_db.commit()
        pending = cache_db.get_pending_changes(check_freshness=False)
        assert len(pending) == 0

    def test_dir_flags(self, cache_db):
        cache_db.set_dir_flag("/test", bulk_copied=True)
        cache_db.commit()
        assert cache_db.is_dir_bulk_copied("/test") is True

        cache_db.set_dir_flag("/test2", bulk_copied=False)
        cache_db.commit()
        assert cache_db.is_dir_bulk_copied("/test2") is False

        assert cache_db.is_dir_bulk_copied("/unknown") is None

    def test_scan_run_tracking(self, cache_db):
        run_id = cache_db.start_scan_run("/photos")
        assert run_id > 0

        cache_db.finish_scan_run(run_id, file_count=100, changes=10)
        # No assertion needed — just verify it doesn't crash

    def test_get_stats(self, cache_db):
        stats = cache_db.get_stats()
        assert stats["total_files"] == 0
        assert stats["proposed_changes"] == 0
        assert stats["applied_changes"] == 0

    def test_upsert_clears_proposed(self, cache_db):
        """Re-scanning a file should clear its proposed change."""
        cache_db.upsert_file(
            path="/test/photo.jpg",
            directory="/test",
            filename="photo.jpg",
            extension="jpg",
            mtime=1609459200.0,
            size=5000,
            metadata={},
        )
        cache_db.set_proposed_change(
            "/test/photo.jpg", {"path": "/test/photo.jpg"}, "high", None,
        )
        cache_db.commit()
        assert len(cache_db.get_pending_changes(check_freshness=False)) == 1

        # Re-upsert (simulates re-scan)
        cache_db.upsert_file(
            path="/test/photo.jpg",
            directory="/test",
            filename="photo.jpg",
            extension="jpg",
            mtime=1609459201.0,  # different mtime
            size=5100,
            metadata={"DateTimeOriginal": "2021:01:01 10:00:00"},
        )
        cache_db.commit()
        assert len(cache_db.get_pending_changes(check_freshness=False)) == 0

    def test_freshness_skips_stale(self, cache_db, tmp_dir):
        """Freshness check should skip proposals when file has changed."""
        # Create a real file
        real_file = tmp_dir / "fresh.jpg"
        real_file.write_bytes(b"\xff\xd8\xff\xd9")
        st = real_file.stat()

        cache_db.upsert_file(
            path=str(real_file),
            directory=str(tmp_dir),
            filename="fresh.jpg",
            extension="jpg",
            mtime=st.st_mtime,
            size=st.st_size,
            metadata={},
        )
        cache_db.set_proposed_change(
            str(real_file), {"path": str(real_file)}, "high", None,
        )
        cache_db.commit()

        # File unchanged → proposal returned
        pending = cache_db.get_pending_changes(check_freshness=True)
        assert len(pending) == 1

        # Modify the file → proposal skipped as stale
        real_file.write_bytes(b"\xff\xd8\xff\xe0\xff\xd9")
        pending = cache_db.get_pending_changes(check_freshness=True)
        assert len(pending) == 0

    def test_freshness_skips_missing_file(self, cache_db):
        """Missing files should be skipped during freshness check."""
        cache_db.upsert_file(
            path="/nonexistent/gone.jpg",
            directory="/nonexistent",
            filename="gone.jpg",
            extension="jpg",
            mtime=1609459200.0,
            size=5000,
            metadata={},
        )
        cache_db.set_proposed_change(
            "/nonexistent/gone.jpg", {"path": "/nonexistent/gone.jpg"}, "high", None,
        )
        cache_db.commit()

        pending = cache_db.get_pending_changes(check_freshness=True)
        assert len(pending) == 0

    def test_root_scoping(self, cache_db):
        """Root filtering should only match proper children."""
        for name, directory in [
            ("a.jpg", "/photos/Albums"),
            ("b.jpg", "/photos/Albums2"),
            ("c.jpg", "/photos/Albums/sub"),
        ]:
            cache_db.upsert_file(
                path=f"{directory}/{name}",
                directory=directory,
                filename=name,
                extension="jpg",
                mtime=1609459200.0,
                size=5000,
                metadata={},
            )
            cache_db.set_proposed_change(
                f"{directory}/{name}", {"path": f"{directory}/{name}"}, "med", None,
            )
        cache_db.commit()

        # Only /photos/Albums and its children, NOT /photos/Albums2
        pending = cache_db.get_pending_changes(
            root="/photos/Albums", check_freshness=False,
        )
        paths = [p["path"] for p in pending]
        assert "/photos/Albums/a.jpg" in paths
        assert "/photos/Albums/sub/c.jpg" in paths
        assert "/photos/Albums2/b.jpg" not in paths
