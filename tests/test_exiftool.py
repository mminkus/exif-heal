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
        assert "-GPSLatitude=-34.5" in content
        assert "-GPSLongitude=138.5" in content

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


class TestWriteViaArgfileOutputParsing:
    """Unit tests for write_via_argfile output parsing (no exiftool needed)."""

    def test_all_updated(self, tmp_dir):
        """All files report '1 image files updated'."""
        from unittest.mock import patch, MagicMock

        mock_result = MagicMock()
        mock_result.stdout = "1 image files updated\n1 image files updated\n"
        mock_result.stderr = ""

        argfile = tmp_dir / "args.txt"
        argfile.write_text("-DateTimeOriginal=2020:01:01 10:00:00\n/test/a.jpg\n-execute\n"
                           "-DateTimeOriginal=2020:01:01 11:00:00\n/test/b.jpg\n-execute\n")

        with patch("exif_heal.exiftool.subprocess.run", return_value=mock_result):
            written, errors, stderr = write_via_argfile(
                argfile, ["/test/a.jpg", "/test/b.jpg"],
            )
        assert written == ["/test/a.jpg", "/test/b.jpg"]
        assert errors == 0

    def test_nothing_to_do_skips_file(self, tmp_dir):
        """'Nothing to do.' lines should be tracked as failed batches."""
        from unittest.mock import patch, MagicMock

        mock_result = MagicMock()
        mock_result.stdout = "1 image files updated\nNothing to do.\n1 image files updated\n"
        mock_result.stderr = ""

        argfile = tmp_dir / "args.txt"
        argfile.write_text("dummy")

        with patch("exif_heal.exiftool.subprocess.run", return_value=mock_result):
            written, errors, stderr = write_via_argfile(
                argfile, ["/test/a.jpg", "/test/b.jpg", "/test/c.jpg"],
            )
        # b.jpg had "Nothing to do." so only a.jpg and c.jpg succeed
        assert written == ["/test/a.jpg", "/test/c.jpg"]

    def test_error_count_parsed(self, tmp_dir):
        """Error summary line is parsed for total count."""
        from unittest.mock import patch, MagicMock

        mock_result = MagicMock()
        mock_result.stdout = "1 image files updated\n1 files weren't updated due to errors\n"
        mock_result.stderr = ""

        argfile = tmp_dir / "args.txt"
        argfile.write_text("dummy")

        with patch("exif_heal.exiftool.subprocess.run", return_value=mock_result):
            written, errors, stderr = write_via_argfile(
                argfile, ["/test/a.jpg"],
            )
        assert written == ["/test/a.jpg"]
        assert errors == 1

    def test_fewer_batches_than_expected(self, tmp_dir):
        """If exiftool produces fewer output lines than files, extras are not marked."""
        from unittest.mock import patch, MagicMock

        mock_result = MagicMock()
        mock_result.stdout = "1 image files updated\n"
        mock_result.stderr = ""

        argfile = tmp_dir / "args.txt"
        argfile.write_text("dummy")

        with patch("exif_heal.exiftool.subprocess.run", return_value=mock_result):
            written, errors, stderr = write_via_argfile(
                argfile, ["/test/a.jpg", "/test/b.jpg"],
            )
        # Only 1 batch result for 2 expected paths — second file not marked
        assert written == ["/test/a.jpg"]


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

        expected_paths = [change["path"] for change in changes]
        successfully_written, errors, stderr = write_via_argfile(argfile, expected_paths)
        assert len(successfully_written) >= 1
        assert str(filepath) in successfully_written
        assert errors == 0

        # Verify the tag was written
        records = batch_read_files([filepath])
        assert len(records) == 1
        dto = get_tag(records[0], "DateTimeOriginal", ["ExifIFD", "IFD0"])
        assert dto is not None
        assert "2020:06:15" in str(dto)
