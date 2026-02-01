"""CLI smoke tests via click.testing.CliRunner."""

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from exif_heal.cli import main


def has_exiftool():
    try:
        r = subprocess.run(["exiftool", "-ver"], capture_output=True, timeout=5)
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


skip_no_exiftool = pytest.mark.skipif(
    not has_exiftool(), reason="exiftool not installed"
)


class TestCLIHelp:

    def test_main_help(self):
        runner = CliRunner()
        result = runner.invoke(main, ["--help"])
        assert result.exit_code == 0
        assert "exif-heal" in result.output

    def test_scan_help(self):
        runner = CliRunner()
        result = runner.invoke(main, ["scan", "--help"])
        assert result.exit_code == 0
        assert "--root" in result.output
        assert "--ext" in result.output
        assert "--exclude-glob" in result.output
        assert "--allow-low-confidence" in result.output
        assert "--gps-hints" in result.output

    def test_apply_help(self):
        runner = CliRunner()
        result = runner.invoke(main, ["apply", "--help"])
        assert result.exit_code == 0
        assert "--commit" in result.output
        assert "--backup-dir" in result.output
        assert "--no-tag-provenance" in result.output
        assert "--no-xmp-mirror" in result.output

    def test_version(self):
        runner = CliRunner()
        result = runner.invoke(main, ["--version"])
        assert result.exit_code == 0
        assert "0.1.0" in result.output


@skip_no_exiftool
class TestScanCommand:

    def test_scan_empty_directory(self, tmp_dir):
        runner = CliRunner()
        report_path = str(tmp_dir / "report.jsonl")
        cache_path = str(tmp_dir / "cache.db")

        result = runner.invoke(main, [
            "scan",
            "--root", str(tmp_dir),
            "--report", report_path,
            "--cache", cache_path,
            "--no-recursive",
        ])
        assert result.exit_code == 0
        assert "scan summary" in result.output.lower()

    def test_scan_with_files(self, tmp_dir, create_jpeg):
        create_jpeg(name="good.jpg", datetime_original="2020:01:01 10:00:00")
        create_jpeg(name="received_20200101_xxx.jpeg")

        runner = CliRunner()
        report_path = str(tmp_dir / "report.jsonl")
        cache_path = str(tmp_dir / "cache.db")

        result = runner.invoke(main, [
            "scan",
            "--root", str(tmp_dir),
            "--report", report_path,
            "--cache", cache_path,
            "--no-recursive",
        ])
        assert result.exit_code == 0

        # Check report exists
        assert Path(report_path).exists()

    def test_scan_with_print_plan(self, tmp_dir, create_jpeg):
        create_jpeg(name="good.jpg", datetime_original="2020:01:01 10:00:00")
        create_jpeg(name="received_20200101_xxx.jpeg")

        runner = CliRunner()
        report_path = str(tmp_dir / "report.jsonl")
        cache_path = str(tmp_dir / "cache.db")

        result = runner.invoke(main, [
            "scan",
            "--root", str(tmp_dir),
            "--report", report_path,
            "--cache", cache_path,
            "--no-recursive",
            "--print-plan",
        ])
        assert result.exit_code == 0

    def test_scan_with_limit(self, tmp_dir, create_jpeg):
        for i in range(5):
            create_jpeg(name=f"received_{20200101 + i}_xxx.jpeg")

        runner = CliRunner()
        report_path = str(tmp_dir / "report.jsonl")
        cache_path = str(tmp_dir / "cache.db")

        result = runner.invoke(main, [
            "scan",
            "--root", str(tmp_dir),
            "--report", report_path,
            "--cache", cache_path,
            "--no-recursive",
            "--limit", "2",
        ])
        assert result.exit_code == 0


@skip_no_exiftool
class TestApplyCommand:

    def test_apply_dry_run(self, tmp_dir, create_jpeg):
        # First scan
        create_jpeg(name="received_20200101_xxx.jpeg")
        runner = CliRunner()
        report_path = str(tmp_dir / "report.jsonl")
        cache_path = str(tmp_dir / "cache.db")

        runner.invoke(main, [
            "scan",
            "--root", str(tmp_dir),
            "--report", report_path,
            "--cache", cache_path,
            "--no-recursive",
        ])

        # Then apply (dry run — default)
        # Use --allow-low-confidence since received_ files get LOW confidence
        # (date-only filename parse) and would otherwise be gated at MED threshold
        result = runner.invoke(main, [
            "apply",
            "--root", str(tmp_dir),
            "--cache", cache_path,
            "--allow-low-confidence",
        ])
        assert result.exit_code == 0
        assert "DRY RUN" in result.output
