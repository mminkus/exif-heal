"""Integration tests for exiftool wrapper — requires exiftool binary."""

import json
import subprocess
import tempfile
from pathlib import Path

import pytest

from exif_heal.exiftool import (
    batch_read_directory,
    batch_read_files,
    generate_argfile,
    get_tag,
    read_gps,
    write_via_argfile,
)

pytestmark = pytest.mark.integration


def has_exiftool():
    try:
        r = subprocess.run(["exiftool", "-ver"], capture_output=True, timeout=5)
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


skip_no_exiftool = pytest.mark.skipif(
    not has_exiftool(), reason="exiftool not installed"
)


class TestGetTag:

    def test_with_group_prefix(self):
        record = {"ExifIFD:DateTimeOriginal": "2020:01:01 10:00:00"}
        assert get_tag(record, "DateTimeOriginal", ["ExifIFD"]) == "2020:01:01 10:00:00"

    def test_multiple_groups(self):
        record = {"IFD0:ModifyDate": "2020:01:01 11:00:00"}
        assert get_tag(record, "ModifyDate", ["ExifIFD", "IFD0"]) == "2020:01:01 11:00:00"

    def test_without_group(self):
        record = {"DateTimeOriginal": "2020:01:01 10:00:00"}
        assert get_tag(record, "DateTimeOriginal", ["ExifIFD"]) == "2020:01:01 10:00:00"

    def test_missing_tag(self):
        record = {"SomethingElse": "value"}
        assert get_tag(record, "DateTimeOriginal", ["ExifIFD"]) is None

    def test_null_value(self):
        record = {"ExifIFD:DateTimeOriginal": None}
        assert get_tag(record, "DateTimeOriginal", ["ExifIFD"]) is None

    def test_empty_string(self):
        record = {"ExifIFD:DateTimeOriginal": ""}
        assert get_tag(record, "DateTimeOriginal", ["ExifIFD"]) is None

    def test_zero_date(self):
        record = {"ExifIFD:DateTimeOriginal": "0000:00:00 00:00:00"}
        assert get_tag(record, "DateTimeOriginal", ["ExifIFD"]) is None


@skip_no_exiftool
class TestBatchRead:

    def test_empty_directory(self, tmp_dir):
        result = batch_read_directory(tmp_dir, ["jpg"])
        assert result == []

    def test_read_jpeg(self, tmp_dir, create_jpeg):
        create_jpeg(
            name="test.jpg",
            datetime_original="2020:01:01 10:00:00",
            gps_lat=-34.5,
            gps_lon=138.5,
            make="samsung",
            model="SM-N950U1",
        )
        records = batch_read_directory(tmp_dir, ["jpg"])
        assert len(records) == 1

        r = records[0]
        assert get_tag(r, "DateTimeOriginal", ["ExifIFD", "IFD0"]) is not None
        assert get_tag(r, "Make", ["IFD0"]) == "samsung"

    def test_read_multiple_files(self, tmp_dir, create_jpeg):
        create_jpeg(name="a.jpg", datetime_original="2020:01:01 10:00:00")
        create_jpeg(name="b.jpg", datetime_original="2020:01:01 11:00:00")
        create_jpeg(name="c.jpg")  # no EXIF

        records = batch_read_directory(tmp_dir, ["jpg"])
        assert len(records) == 3

    def test_extension_filter(self, tmp_dir, create_jpeg):
        create_jpeg(name="test.jpg")
        # Create a non-matching file
        (tmp_dir / "test.txt").write_text("not a photo")

        records = batch_read_directory(tmp_dir, ["jpg"])
        assert len(records) == 1


@skip_no_exiftool
class TestBatchReadFiles:

    def test_read_specific_files(self, tmp_dir, create_jpeg):
        f1 = create_jpeg(name="a.jpg", datetime_original="2020:01:01 10:00:00")
        f2 = create_jpeg(name="b.jpg")

        records = batch_read_files([f1, f2])
        assert len(records) == 2


class TestGenerateArgfile:

    def test_time_only(self):
        changes = [{
            "path": "/test/photo.jpg",
            "time": {
                "datetime_original": "2020:01:01 10:00:00",
                "create_date": "2020:01:01 10:00:00",
                "modify_date": None,
            },
        }]
        content = generate_argfile(changes, tag_provenance=False, xmp_mirror=False)
        assert "-DateTimeOriginal=2020:01:01 10:00:00" in content
        assert "-CreateDate=2020:01:01 10:00:00" in content
        assert "-ModifyDate" not in content
        assert "/test/photo.jpg" in content
        assert "-execute" in content

    def test_gps_only(self):
        changes = [{
            "path": "/test/photo.jpg",
            "gps": {"lat": -34.5, "lon": 138.5},
        }]
        content = generate_argfile(changes, tag_provenance=False, xmp_mirror=False)
        # "*" suffix ensures exiftool also writes the hemisphere Ref tags.
        assert "-GPSLatitude*=-34.5" in content
        assert "-GPSLongitude*=138.5" in content

    def test_with_provenance(self):
        changes = [{
            "path": "/test/photo.jpg",
            "time": {"datetime_original": "2020:01:01 10:00:00", "create_date": "2020:01:01 10:00:00", "modify_date": None},
            "provenance": {
                "time_source": "neighbor_interp",
                "time_confidence": "high",
                "gps_source": "none",
                "gps_confidence": "none",
            },
        }]
        content = generate_argfile(changes, tag_provenance=True, xmp_mirror=False)
        assert "ExifHealTimeSource=neighbor_interp" in content
        assert "ExifHealTimeConfidence=high" in content

    def test_with_xmp_mirror(self):
        changes = [{
            "path": "/test/photo.jpg",
            "time": {"datetime_original": "2020:01:01 10:00:00", "create_date": "2020:01:01 10:00:00", "modify_date": None},
            "gps": {"lat": -34.5, "lon": 138.5},
        }]
        content = generate_argfile(changes, tag_provenance=False, xmp_mirror=True)
        assert "XMP-xmp:DateCreated=" in content
        assert "XMP-photoshop:DateCreated=" in content
        assert "XMP-exif:GPSLatitude=" in content

    def test_multiple_files(self):
        changes = [
            {"path": "/test/a.jpg", "time": {"datetime_original": "2020:01:01 10:00:00", "create_date": "2020:01:01 10:00:00", "modify_date": None}},
            {"path": "/test/b.jpg", "gps": {"lat": -34.5, "lon": 138.5}},
        ]
        content = generate_argfile(changes, tag_provenance=False, xmp_mirror=False)
        assert content.count("-execute") == 2
        assert "/test/a.jpg" in content
        assert "/test/b.jpg" in content

    def test_video_uses_quicktime_tags(self):
        """Video changes must write QuickTime/Keys tags, not ExifIFD/EXIF GPS."""
        changes = [{
            "path": "/test/clip.mp4",
            "time": {"datetime_original": "2019:05:20 14:30:00",
                     "create_date": "2019:05:20 14:30:00",
                     "modify_date": "2019:05:20 14:30:00"},
            "gps": {"lat": -34.93, "lon": 138.61},
        }]
        content = generate_argfile(changes, tag_provenance=False, xmp_mirror=True)
        # QuickTime time tags
        assert "-QuickTime:CreateDate=2019:05:20 14:30:00" in content
        assert "-TrackCreateDate=2019:05:20 14:30:00" in content
        assert "-QuickTime:ModifyDate=2019:05:20 14:30:00" in content
        # QuickTime GPS as a coordinate string
        assert "-Keys:GPSCoordinates=-34.93 138.61" in content
        # Must NOT emit photo-only tags for a video
        assert "-DateTimeOriginal=" not in content
        assert "-GPSLatitude" not in content
        assert "XMP-exif:GPSLatitude" not in content


class TestWriteViaArgfileVerification:
    """Unit tests for write_via_argfile success verification (no exiftool needed).

    Success is decided by re-reading each file and confirming the intended
    values landed, so these tests mock both the exiftool run and the re-read.
    """

    def _run(self, tmp_dir, expected_changes, read_records):
        from unittest.mock import patch, MagicMock

        mock_result = MagicMock()
        mock_result.stdout = ""
        mock_result.stderr = ""

        argfile = tmp_dir / "args.txt"
        argfile.write_text("dummy")

        with patch("exif_heal.exiftool.subprocess.run", return_value=mock_result), \
             patch("exif_heal.exiftool.batch_read_files", return_value=read_records):
            return write_via_argfile(argfile, expected_changes)

    def test_all_landed(self, tmp_dir):
        changes = [
            {"path": "/test/a.jpg", "time": {"datetime_original": "2020:01:01 10:00:00"}},
            {"path": "/test/b.jpg", "time": {"datetime_original": "2020:01:01 11:00:00"}},
        ]
        records = [
            {"SourceFile": "/test/a.jpg", "ExifIFD:DateTimeOriginal": "2020:01:01 10:00:00"},
            {"SourceFile": "/test/b.jpg", "ExifIFD:DateTimeOriginal": "2020:01:01 11:00:00"},
        ]
        written, errors, _ = self._run(tmp_dir, changes, records)
        assert written == ["/test/a.jpg", "/test/b.jpg"]
        assert errors == 0

    def test_noop_file_not_marked(self, tmp_dir):
        """A file whose intended value did NOT land is not marked written,
        and crucially does not desync the mapping for the others."""
        changes = [
            {"path": "/test/a.jpg", "time": {"datetime_original": "2020:01:01 10:00:00"}},
            {"path": "/test/b.jpg", "time": {"datetime_original": "2020:01:01 11:00:00"}},
            {"path": "/test/c.jpg", "time": {"datetime_original": "2020:01:01 12:00:00"}},
        ]
        records = [
            {"SourceFile": "/test/a.jpg", "ExifIFD:DateTimeOriginal": "2020:01:01 10:00:00"},
            # b.jpg unchanged (still missing the tag) -> not written
            {"SourceFile": "/test/b.jpg"},
            {"SourceFile": "/test/c.jpg", "ExifIFD:DateTimeOriginal": "2020:01:01 12:00:00"},
        ]
        written, errors, _ = self._run(tmp_dir, changes, records)
        assert written == ["/test/a.jpg", "/test/c.jpg"]
        assert errors == 1

    def test_missing_readback_not_marked(self, tmp_dir):
        """A file absent from the re-read is treated as failed."""
        changes = [{"path": "/test/a.jpg", "time": {"datetime_original": "2020:01:01 10:00:00"}}]
        written, errors, _ = self._run(tmp_dir, changes, [])
        assert written == []
        assert errors == 1

    def test_signed_gps_landed(self, tmp_dir):
        """GPS verification uses the SIGNED value (Composite), so a southern
        coordinate must match its negative intended value."""
        changes = [{"path": "/test/a.jpg", "gps": {"lat": -34.5, "lon": 138.6}}]
        records = [{
            "SourceFile": "/test/a.jpg",
            "GPS:GPSLatitude": 34.5,           # unsigned magnitude
            "GPS:GPSLongitude": 138.6,
            "Composite:GPSLatitude": -34.5,    # signed
            "Composite:GPSLongitude": 138.6,
        }]
        written, errors, _ = self._run(tmp_dir, changes, records)
        assert written == ["/test/a.jpg"]
        assert errors == 0

    def test_wrong_gps_not_marked(self, tmp_dir):
        changes = [{"path": "/test/a.jpg", "gps": {"lat": -34.5, "lon": 138.6}}]
        records = [{
            "SourceFile": "/test/a.jpg",
            "Composite:GPSLatitude": 34.5,     # sign flipped -> mismatch
            "Composite:GPSLongitude": 138.6,
        }]
        written, errors, _ = self._run(tmp_dir, changes, records)
        assert written == []
        assert errors == 1


@skip_no_exiftool
class TestWriteViaArgfile:

    def test_write_datetime(self, tmp_dir, create_jpeg):
        filepath = create_jpeg(name="test.jpg")

        changes = [{
            "path": str(filepath),
            "time": {
                "datetime_original": "2020:06:15 14:30:00",
                "create_date": "2020:06:15 14:30:00",
                "modify_date": "2020:06:15 14:30:00",
            },
        }]
        content = generate_argfile(changes, tag_provenance=False, xmp_mirror=False)

        argfile = tmp_dir / "args.txt"
        argfile.write_text(content)

        successfully_written, errors, stderr = write_via_argfile(argfile, changes)
        assert len(successfully_written) >= 1
        assert str(filepath) in successfully_written
        assert errors == 0

        # Verify the tag was written
        records = batch_read_files([filepath])
        assert len(records) == 1
        dto = get_tag(records[0], "DateTimeOriginal", ["ExifIFD", "IFD0"])
        assert dto is not None
        assert "2020:06:15" in str(dto)


def _has_ffmpeg():
    try:
        r = subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=5)
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


skip_no_ffmpeg = pytest.mark.skipif(
    not (has_exiftool() and _has_ffmpeg()),
    reason="ffmpeg + exiftool required for video tests",
)


@skip_no_ffmpeg
class TestVideoReadWrite:

    def test_read_video_time_and_gps(self, tmp_dir, create_video):
        """A video's QuickTime time + signed GPS is read correctly."""
        f = create_video(
            name="clip.mov",
            create_date="2019:05:20 14:30:00",
            gps_lat=-34.93,
            gps_lon=138.61,
        )
        records = batch_read_files([f])
        assert len(records) == 1
        r = records[0]
        assert get_tag(r, "CreateDate", ["QuickTime"]) is not None
        # Signed GPS via Composite (southern hemisphere stays negative)
        coords = read_gps(r)
        assert coords is not None
        assert coords[0] == pytest.approx(-34.93, abs=1e-3)
        assert coords[1] == pytest.approx(138.61, abs=1e-3)

    def test_write_video_time_and_gps_round_trip(self, tmp_dir, create_video):
        """Writing via the QuickTime branch round-trips through verification."""
        f = create_video(name="clean.mp4")  # no metadata

        changes = [{
            "path": str(f),
            "time": {"datetime_original": "2018:07:01 10:05:00",
                     "create_date": "2018:07:01 10:05:00",
                     "modify_date": "2018:07:01 10:05:00"},
            "gps": {"lat": -34.93, "lon": 138.61},
        }]
        argfile = tmp_dir / "args.txt"
        argfile.write_text(generate_argfile(changes, tag_provenance=False, xmp_mirror=False))

        written, errors, _ = write_via_argfile(argfile, changes)
        assert written == [str(f)]
        assert errors == 0

        # Confirm on disk
        r = batch_read_files([f])[0]
        assert "2018:07:01 10:05:00" in str(get_tag(r, "CreateDate", ["QuickTime"]))
        coords = read_gps(r)
        assert coords[0] == pytest.approx(-34.93, abs=1e-3)
