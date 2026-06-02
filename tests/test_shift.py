"""Tests for the timezone-correction shift feature."""

import subprocess
from datetime import datetime, timedelta

import pytest

from exif_heal.shifter import (
    make_offset_transform,
    make_zone_transform,
    parse_offset,
    run_shift,
)


def has_exiftool():
    try:
        r = subprocess.run(["exiftool", "-ver"], capture_output=True, timeout=5)
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


skip_no_exiftool = pytest.mark.skipif(
    not has_exiftool(), reason="exiftool not installed"
)


class TestParseOffset:

    def test_hours_only(self):
        assert parse_offset("+3") == timedelta(hours=3)

    def test_hours_minutes(self):
        assert parse_offset("+3:00") == timedelta(hours=3)
        assert parse_offset("-5:30") == timedelta(hours=-5, minutes=-30)

    def test_hours_minutes_seconds(self):
        assert parse_offset("+9:00:00") == timedelta(hours=9)

    def test_negative(self):
        assert parse_offset("-8:00") == timedelta(hours=-8)

    @pytest.mark.parametrize("bad", ["3:00", "abc", "++3", "", "+3:5"])
    def test_invalid(self, bad):
        with pytest.raises(ValueError):
            parse_offset(bad)


class TestOffsetTransform:

    def test_adds_delta(self):
        t = make_offset_transform(timedelta(hours=9))
        assert t(datetime(2018, 7, 1, 14, 0, 0)) == datetime(2018, 7, 1, 23, 0, 0)

    def test_crosses_midnight(self):
        t = make_offset_transform(timedelta(hours=9))
        assert t(datetime(2018, 7, 1, 20, 0, 0)) == datetime(2018, 7, 2, 5, 0, 0)


class TestZoneTransform:

    def test_pst_to_cet_summer(self):
        """Camera on PST while physically in Berlin (CET/CEST), summer = +9h."""
        t = make_zone_transform("America/Los_Angeles", "Europe/Berlin")
        assert t(datetime(2018, 7, 1, 14, 0, 0)) == datetime(2018, 7, 1, 23, 0, 0)

    def test_pst_to_cet_winter(self):
        """Winter is also +9h (both shift by one hour of DST)."""
        t = make_zone_transform("America/Los_Angeles", "Europe/Berlin")
        assert t(datetime(2018, 1, 1, 14, 0, 0)) == datetime(2018, 1, 1, 23, 0, 0)

    def test_unknown_zone_raises(self):
        with pytest.raises(ValueError):
            make_zone_transform("Not/AZone", "Europe/Berlin")


@skip_no_exiftool
class TestRunShift:

    def test_dry_run_does_not_modify(self, tmp_dir, create_jpeg):
        create_jpeg(name="raw1.jpg", datetime_original="2018:07:01 14:00:00")
        transform = make_offset_transform(timedelta(hours=9))
        summary = run_shift(
            tmp_dir, ["jpg"], transform, recursive=False, commit=False,
        )
        assert summary.dry_run is True
        # File unchanged on disk
        from exif_heal.exiftool import batch_read_files, get_tag
        r = batch_read_files([tmp_dir / "raw1.jpg"])[0]
        assert "14:00:00" in str(get_tag(r, "DateTimeOriginal", ["ExifIFD", "IFD0"]))

    def test_commit_offset_shift(self, tmp_dir, create_jpeg):
        create_jpeg(name="raw1.jpg", datetime_original="2018:07:01 14:00:00")
        transform = make_offset_transform(timedelta(hours=9))
        summary = run_shift(
            tmp_dir, ["jpg"], transform, recursive=False, commit=True,
        )
        assert summary.written == 1
        assert summary.errors == 0
        from exif_heal.exiftool import batch_read_files, get_tag
        r = batch_read_files([tmp_dir / "raw1.jpg"])[0]
        assert "2018:07:01 23:00:00" in str(
            get_tag(r, "DateTimeOriginal", ["ExifIFD", "IFD0"])
        )

    def test_commit_zone_shift(self, tmp_dir, create_jpeg):
        create_jpeg(name="raw1.jpg", datetime_original="2018:07:01 14:00:00")
        transform = make_zone_transform("America/Los_Angeles", "Europe/Berlin")
        summary = run_shift(tmp_dir, ["jpg"], transform, recursive=False, commit=True)
        assert summary.written == 1
        from exif_heal.exiftool import batch_read_files, get_tag
        r = batch_read_files([tmp_dir / "raw1.jpg"])[0]
        assert "2018:07:01 23:00:00" in str(
            get_tag(r, "DateTimeOriginal", ["ExifIFD", "IFD0"])
        )

    def test_skips_files_without_time(self, tmp_dir, create_jpeg):
        create_jpeg(name="notime.jpg")  # no EXIF time
        transform = make_offset_transform(timedelta(hours=1))
        summary = run_shift(tmp_dir, ["jpg"], transform, recursive=False, commit=True)
        assert summary.files_skipped_no_time == 1
        assert summary.written == 0

    def test_make_filter(self, tmp_dir, create_jpeg):
        create_jpeg(name="sony.jpg", datetime_original="2018:07:01 14:00:00",
                    make="SONY", model="ILCE-7M3")
        create_jpeg(name="canon.jpg", datetime_original="2018:07:01 14:00:00",
                    make="Canon", model="EOS R5")
        transform = make_offset_transform(timedelta(hours=9))
        summary = run_shift(
            tmp_dir, ["jpg"], transform, recursive=False, commit=True,
            make_filter="sony",
        )
        assert summary.written == 1
        assert summary.files_skipped_filter == 1
        from exif_heal.exiftool import batch_read_files, get_tag
        canon = batch_read_files([tmp_dir / "canon.jpg"])[0]
        # Canon untouched
        assert "14:00:00" in str(get_tag(canon, "DateTimeOriginal", ["ExifIFD", "IFD0"]))

    def test_backup_made(self, tmp_dir, create_jpeg):
        create_jpeg(name="raw1.jpg", datetime_original="2018:07:01 14:00:00")
        backup = tmp_dir / "backups"
        transform = make_offset_transform(timedelta(hours=9))
        summary = run_shift(
            tmp_dir, ["jpg"], transform, recursive=False, commit=True,
            backup_dir=backup,
        )
        assert summary.backed_up == 1
        assert (backup / "raw1.jpg").exists()
