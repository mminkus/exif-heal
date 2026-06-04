"""Tests for apply-time provenance filtering and the apply commit path."""

import subprocess
from datetime import datetime

import pytest

from exif_heal.applier import _filter_provenance, apply_changes
from exif_heal.cache import MetadataCache
from exif_heal.exiftool import batch_read_files, get_tag, read_gps
from exif_heal.models import Confidence, ScanConfig
from exif_heal.scanner import scan


def _has_exiftool():
    try:
        return subprocess.run(["exiftool", "-ver"], capture_output=True,
                              timeout=5).returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


skip_no_exiftool = pytest.mark.skipif(not _has_exiftool(), reason="exiftool not installed")

FULL = {
    "time_source": "neighbor_interp", "time_confidence": "high",
    "gps_source": "default_hint", "gps_confidence": "low",
}


class TestFilterProvenance:

    def test_both_written_keeps_all(self):
        assert _filter_provenance(FULL, wrote_time=True, wrote_gps=True) == FULL

    def test_time_only_drops_gps_provenance(self):
        # Codex repro: high-conf time + gated LOW GPS -> must NOT stamp GPSSource.
        out = _filter_provenance(FULL, wrote_time=True, wrote_gps=False)
        assert out == {"time_source": "neighbor_interp", "time_confidence": "high"}
        assert "gps_source" not in out and "gps_confidence" not in out

    def test_gps_only_drops_time_provenance(self):
        out = _filter_provenance(FULL, wrote_time=False, wrote_gps=True)
        assert out == {"gps_source": "default_hint", "gps_confidence": "low"}

    def test_nothing_written_returns_none(self):
        assert _filter_provenance(FULL, wrote_time=False, wrote_gps=False) is None


# --- apply_changes commit-path integration -----------------------------------


@skip_no_exiftool
class TestApplyChangesCommit:
    """Full apply path: cache -> argfile -> exiftool write -> verify -> mark."""

    def _seed_cache(self, tmp_dir, create_jpeg):
        # anchors + a gap file missing time+GPS
        create_jpeg(name="a.jpg", datetime_original="2020:01:01 10:00:00",
                    gps_lat=-34.50, gps_lon=138.50)
        create_jpeg(name="c.jpg", datetime_original="2020:01:01 10:10:00",
                    gps_lat=-34.51, gps_lon=138.51)
        create_jpeg(name="gap.jpg", mtime=datetime(2020, 1, 1, 10, 5, 0))
        cache_path = tmp_dir / "c.db"
        config = ScanConfig(root=tmp_dir, extensions=["jpg"],
                            min_confidence_time=Confidence.LOW,
                            min_confidence_gps=Confidence.LOW)
        with MetadataCache(cache_path) as cache:
            with open(tmp_dir / "r.jsonl", "w") as rf:
                scan(config, cache, rf)
        return cache_path

    def _gap_record(self, tmp_dir):
        return batch_read_files([tmp_dir / "gap.jpg"])[0]

    def test_dry_run_writes_nothing(self, tmp_dir, create_jpeg):
        cache_path = self._seed_cache(tmp_dir, create_jpeg)
        with MetadataCache(cache_path) as cache:
            summary = apply_changes(cache, root=tmp_dir, commit=False,
                                    min_confidence_time=Confidence.LOW,
                                    min_confidence_gps=Confidence.LOW)
        assert summary.dry_run is True
        rec = self._gap_record(tmp_dir)
        assert get_tag(rec, "DateTimeOriginal", ["ExifIFD", "IFD0"]) is None
        assert read_gps(rec) is None

    def test_commit_writes_and_marks_applied(self, tmp_dir, create_jpeg):
        cache_path = self._seed_cache(tmp_dir, create_jpeg)
        with MetadataCache(cache_path) as cache:
            summary = apply_changes(cache, root=tmp_dir, commit=True,
                                    min_confidence_time=Confidence.LOW,
                                    min_confidence_gps=Confidence.LOW)
        assert summary.written >= 1
        assert summary.errors == 0

        rec = self._gap_record(tmp_dir)
        assert get_tag(rec, "DateTimeOriginal", ["ExifIFD", "IFD0"]) is not None
        coords = read_gps(rec)
        assert coords is not None and coords[0] < 0  # signed southern GPS
        # provenance landed
        assert get_tag(rec, "GPSSource", ["XMP-exifheal"]) is not None

        # gap.jpg now marked applied -> no longer pending
        with MetadataCache(cache_path) as cache:
            pend = cache.get_pending_changes(root=str(tmp_dir.resolve()),
                                             check_freshness=False)
        assert not any(p["path"].endswith("gap.jpg") for p in pend)

    def test_reapply_is_idempotent(self, tmp_dir, create_jpeg):
        cache_path = self._seed_cache(tmp_dir, create_jpeg)
        with MetadataCache(cache_path) as cache:
            apply_changes(cache, root=tmp_dir, commit=True,
                          min_confidence_time=Confidence.LOW,
                          min_confidence_gps=Confidence.LOW)
        with MetadataCache(cache_path) as cache:
            second = apply_changes(cache, root=tmp_dir, commit=True,
                                   min_confidence_time=Confidence.LOW,
                                   min_confidence_gps=Confidence.LOW)
        assert second.written == 0  # nothing left to do
