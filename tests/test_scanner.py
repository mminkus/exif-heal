"""Tests for scanner: datetime parsing, record mapping, and the scan pipeline."""

import subprocess
from datetime import datetime

import pytest

from exif_heal.cache import MetadataCache
from exif_heal.models import Confidence, ScanConfig, TimeSource
from exif_heal.scanner import parse_exiftool_datetime, record_from_exiftool, scan


def _has_exiftool():
    try:
        return subprocess.run(["exiftool", "-ver"], capture_output=True,
                              timeout=5).returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


skip_no_exiftool = pytest.mark.skipif(not _has_exiftool(), reason="exiftool not installed")


class TestParseExiftoolDatetime:

    def test_basic_colon_format(self):
        assert parse_exiftool_datetime("2019:01:21 20:34:43") == datetime(2019, 1, 21, 20, 34, 43)

    def test_dash_format(self):
        assert parse_exiftool_datetime("2019-01-21 20:34:43") == datetime(2019, 1, 21, 20, 34, 43)

    def test_positive_offset_stripped(self):
        assert parse_exiftool_datetime("2019:01:21 20:34:43+05:30") == datetime(2019, 1, 21, 20, 34, 43)

    def test_negative_offset_stripped(self):
        # Regression: colon-format negative offset previously failed to parse.
        assert parse_exiftool_datetime("2019:01:21 20:34:43-08:00") == datetime(2019, 1, 21, 20, 34, 43)

    def test_zulu_and_t_separator(self):
        assert parse_exiftool_datetime("2019:01:21T20:34:43Z") == datetime(2019, 1, 21, 20, 34, 43)

    def test_fractional_seconds_dropped(self):
        assert parse_exiftool_datetime("2019:01:21 20:34:43.123") == datetime(2019, 1, 21, 20, 34, 43)

    def test_date_only_is_midnight(self):
        assert parse_exiftool_datetime("2019:01:21") == datetime(2019, 1, 21, 0, 0, 0)

    @pytest.mark.parametrize("val", [None, "", "0000:00:00 00:00:00", "garbage", "not a date"])
    def test_unparseable_returns_none(self, val):
        assert parse_exiftool_datetime(val) is None


class TestRecordFromExiftool:
    """Maps a raw exiftool JSON dict to a FileRecord. Files need not exist on
    disk; we assert the metadata mapping (GPS sign, time hierarchy, video)."""

    def test_signed_gps_via_composite(self):
        rec = record_from_exiftool({
            "SourceFile": "/x/a.jpg",
            "Composite:GPSLatitude": -34.5,
            "Composite:GPSLongitude": 138.6,
            "ExifIFD:DateTimeOriginal": "2020:01:01 10:00:00",
        })
        assert rec.gps is not None
        assert rec.gps.lat == pytest.approx(-34.5)
        assert rec.gps.lon == pytest.approx(138.6)

    def test_signed_gps_via_magnitude_and_ref(self):
        # Southern + western hemisphere from unsigned magnitude + Ref.
        rec = record_from_exiftool({
            "SourceFile": "/x/a.jpg",
            "GPS:GPSLatitude": 34.5, "GPS:GPSLongitude": 138.6,
            "GPS:GPSLatitudeRef": "S", "GPS:GPSLongitudeRef": "W",
        })
        assert rec.gps.lat == pytest.approx(-34.5)
        assert rec.gps.lon == pytest.approx(-138.6)

    def test_no_gps(self):
        rec = record_from_exiftool({"SourceFile": "/x/a.jpg"})
        assert rec.gps is None
        assert rec.has_gps is False

    def test_capture_time_hierarchy_dto_wins(self):
        rec = record_from_exiftool({
            "SourceFile": "/x/a.jpg",
            "ExifIFD:DateTimeOriginal": "2020:01:01 10:00:00",
            "ExifIFD:CreateDate": "2019:01:01 10:00:00",
        })
        assert rec.capture_time == datetime(2020, 1, 1, 10, 0, 0)
        assert rec.capture_time_source == TimeSource.EXIF_DTO

    def test_capture_time_falls_to_create_then_modify(self):
        only_create = record_from_exiftool({
            "SourceFile": "/x/a.jpg", "ExifIFD:CreateDate": "2019:01:01 10:00:00"})
        assert only_create.capture_time_source == TimeSource.EXIF_CREATE
        only_modify = record_from_exiftool({
            "SourceFile": "/x/a.jpg", "IFD0:ModifyDate": "2019:01:01 10:00:00"})
        assert only_modify.capture_time_source == TimeSource.EXIF_MODIFY

    def test_filename_time_when_no_exif(self):
        rec = record_from_exiftool({"SourceFile": "/x/IMG_20180815_143000.jpg"})
        assert rec.capture_time == datetime(2018, 8, 15, 14, 30, 0)
        assert rec.capture_time_source == TimeSource.FILENAME

    def test_video_time_from_quicktime(self):
        rec = record_from_exiftool({
            "SourceFile": "/x/clip.mp4",
            "QuickTime:CreateDate": "2019:05:20 14:30:00",
        })
        assert rec.is_video is True
        assert rec.has_exif_time is True
        assert rec.capture_time == datetime(2019, 5, 20, 14, 30, 0)

    def test_video_prefers_keys_creationdate(self):
        rec = record_from_exiftool({
            "SourceFile": "/x/clip.mov",
            "Keys:CreationDate": "2019:05:20 14:30:00+09:30",
            "QuickTime:CreateDate": "2019:05:20 05:00:00",
        })
        # Keys:CreationDate preferred; offset stripped to naive local.
        assert rec.capture_time == datetime(2019, 5, 20, 14, 30, 0)


@skip_no_exiftool
class TestScanPipeline:
    """End-to-end: walk -> read -> infer -> gate -> cache + report."""

    def _scan(self, tmp_dir, **cfg):
        report = tmp_dir / "r.jsonl"
        cache_path = tmp_dir / "c.db"
        config = ScanConfig(
            root=tmp_dir, extensions=["jpg"],
            min_confidence_time=Confidence.LOW, min_confidence_gps=Confidence.LOW,
            **cfg,
        )
        with MetadataCache(cache_path) as cache:
            with open(report, "w") as rf:
                summary = scan(config, cache, rf)
            pending = cache.get_pending_changes(
                root=str(tmp_dir.resolve()), check_freshness=False)
        return summary, pending, report.read_text()

    def _seed(self, create_jpeg):
        # Two anchors (time+GPS) bracketing a gap file missing both.
        create_jpeg(name="a.jpg", datetime_original="2020:01:01 10:00:00",
                    gps_lat=-34.50, gps_lon=138.50)
        create_jpeg(name="c.jpg", datetime_original="2020:01:01 10:10:00",
                    gps_lat=-34.51, gps_lon=138.51)
        create_jpeg(name="gap.jpg", mtime=datetime(2020, 1, 1, 10, 5, 0))

    def test_pipeline_proposes_time_and_gps_for_gap(self, tmp_dir, create_jpeg):
        self._seed(create_jpeg)
        summary, pending, report = self._scan(tmp_dir)
        by_name = {p["path"].split("/")[-1]: p for p in pending}
        assert "gap.jpg" in by_name
        assert "time" in by_name["gap.jpg"]
        assert "gps" in by_name["gap.jpg"]
        # GPS lands in the donor region (southern hemisphere preserved).
        assert by_name["gap.jpg"]["gps"]["lat"] < 0
        assert '"file"' in report and "gap.jpg" in report

    def test_only_missing_time_skips_gps(self, tmp_dir, create_jpeg):
        self._seed(create_jpeg)
        _, pending, _ = self._scan(tmp_dir, only_missing_time=True)
        gap = next(p for p in pending if p["path"].endswith("gap.jpg"))
        assert "time" in gap and "gps" not in gap

    def test_rescan_clears_stale_proposal_after_file_gains_metadata(self, tmp_dir, create_jpeg):
        self._seed(create_jpeg)
        report = tmp_dir / "r.jsonl"
        cache_path = tmp_dir / "c.db"
        config = ScanConfig(root=tmp_dir, extensions=["jpg"],
                            min_confidence_time=Confidence.LOW,
                            min_confidence_gps=Confidence.LOW)
        with MetadataCache(cache_path) as cache:
            with open(report, "w") as rf:
                scan(config, cache, rf)
            before = cache.get_pending_changes(root=str(tmp_dir.resolve()),
                                               check_freshness=False)
            assert any(p["path"].endswith("gap.jpg") for p in before)

            # gap.jpg now gains real metadata, then re-scan.
            subprocess.run(["exiftool", "-overwrite_original",
                            "-DateTimeOriginal=2020:01:01 10:05:00",
                            "-GPSLatitude*=-34.505", "-GPSLongitude*=138.505",
                            str(tmp_dir / "gap.jpg")], capture_output=True, timeout=10)
            with open(report, "w") as rf:
                scan(config, cache, rf)
            after = cache.get_pending_changes(root=str(tmp_dir.resolve()),
                                              check_freshness=False)

        # The stale gap.jpg proposal is gone (no longer missing metadata).
        assert not any(p["path"].endswith("gap.jpg") for p in after)
