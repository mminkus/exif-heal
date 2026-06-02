"""Tests for parallel scan: jobs resolver + determinism across worker counts."""

import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from exif_heal.cache import MetadataCache
from exif_heal.models import Confidence, ScanConfig
from exif_heal.scanner import resolve_jobs, scan


class TestResolveJobs:

    def test_explicit_positive(self):
        assert resolve_jobs(4, 100) == 4

    def test_capped_at_num_dirs(self):
        assert resolve_jobs(64, 3) == 3

    def test_auto_uses_cpu_count(self, monkeypatch):
        monkeypatch.setattr("exif_heal.scanner.os.cpu_count", lambda: 8)
        assert resolve_jobs(None, 100) == 8
        assert resolve_jobs(0, 100) == 8

    def test_auto_cpu_count_none_falls_back(self, monkeypatch):
        monkeypatch.setattr("exif_heal.scanner.os.cpu_count", lambda: None)
        assert resolve_jobs(None, 100) == 4

    def test_zero_dirs(self):
        assert resolve_jobs(8, 0) == 1

    def test_never_below_one(self):
        assert resolve_jobs(1, 1) == 1


def has_exiftool():
    try:
        r = subprocess.run(["exiftool", "-ver"], capture_output=True, timeout=5)
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


@pytest.mark.skipif(not has_exiftool(), reason="exiftool not installed")
class TestScanDeterminism:

    def _run(self, tmp_dir, jobs):
        report = tmp_dir / f"report_j{jobs}.jsonl"
        cache_path = tmp_dir / f"cache_j{jobs}.db"
        config = ScanConfig(
            root=tmp_dir,
            extensions=["jpg"],
            recursive=True,
            min_confidence_time=Confidence.LOW,
            min_confidence_gps=Confidence.LOW,
        )
        with MetadataCache(cache_path) as cache:
            with open(report, "w") as rf:
                scan(config, cache, rf, jobs=jobs)
        return report.read_text()

    def test_identical_report_across_worker_counts(self, tmp_dir, create_jpeg):
        # Multi-directory tree. Each dir: two time+GPS anchors (10:00 and 10:10)
        # and a gap file whose mtime sits between them, so the gap deterministically
        # interpolates time and copies GPS from a neighbor.
        for i, sub in enumerate(["CityA", "CityB", "CityC", "CityD"]):
            create_jpeg(name="a_anchor1.jpg", subdir=sub,
                        datetime_original=f"2019:05:2{i} 10:00:00",
                        gps_lat=-34.90 - i * 0.01, gps_lon=138.60 + i * 0.01)
            create_jpeg(name="c_anchor2.jpg", subdir=sub,
                        datetime_original=f"2019:05:2{i} 10:10:00",
                        gps_lat=-34.91 - i * 0.01, gps_lon=138.61 + i * 0.01)
            create_jpeg(name="b_gap.jpg", subdir=sub,
                        mtime=datetime(2019, 5, 20 + i, 10, 5, 0))

        serial = self._run(tmp_dir, jobs=1)
        parallel = self._run(tmp_dir, jobs=4)

        # Reports are written in directory order; parallel reads must not change
        # the serialized output at all.
        assert serial == parallel
        assert serial.strip()  # non-empty (proposals were made)
