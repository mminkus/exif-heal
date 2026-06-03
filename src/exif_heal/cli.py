"""CLI entry point: scan + apply subcommands."""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime
from pathlib import Path

import click

from .applier import apply_changes
from .cache import MetadataCache
from .confidence import parse_confidence
from .models import Confidence, GPSCoord, GPSHint, ScanConfig
from .scanner import scan
from .shifter import make_offset_transform, make_zone_transform, parse_offset, run_shift

DEFAULT_EXTENSIONS = "jpg,jpeg,dng,heic,png,mp4,mov,m4v,3gp"


def _parse_gps(value: str) -> GPSCoord:
    """Parse 'lat,lon' string to GPSCoord."""
    try:
        parts = value.split(",")
        return GPSCoord(lat=float(parts[0].strip()), lon=float(parts[1].strip()))
    except (ValueError, IndexError):
        raise click.BadParameter(f"GPS must be 'lat,lon', got: {value}")


def _load_gps_hints(path: str) -> list[GPSHint]:
    """Load GPS hints from a JSON file."""
    try:
        with open(path) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        raise click.BadParameter(f"Failed to load GPS hints: {e}")

    hints = []
    for entry in data:
        try:
            hints.append(GPSHint(
                date_from=datetime.strptime(entry["from"], "%Y-%m-%d"),
                date_to=datetime.strptime(entry["to"], "%Y-%m-%d").replace(
                    hour=23, minute=59, second=59,
                ),
                coord=GPSCoord(lat=float(entry["lat"]), lon=float(entry["lon"])),
                label=entry.get("label", f"{entry['lat']},{entry['lon']}"),
            ))
        except (KeyError, ValueError) as e:
            raise click.BadParameter(f"Invalid GPS hint entry: {e}")

    return hints


def _setup_logging(verbose: bool):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(levelname)s: %(message)s",
        stream=sys.stderr,
    )


@click.group()
@click.version_option(package_name="exif-heal")
def main():
    """exif-heal: repair missing EXIF timestamps and GPS data."""
    pass


@main.command()
@click.option("--root", required=True, type=click.Path(exists=True, file_okay=False),
              help="Root directory to scan")
@click.option("--ext", default=DEFAULT_EXTENSIONS,
              help="Comma-separated file extensions")
@click.option("--recursive/--no-recursive", default=True,
              help="Recurse into subdirectories")
@click.option("--exclude-glob", multiple=True,
              help="Glob pattern for dirs/files to skip (repeatable)")
@click.option("--no-default-excludes", is_flag=True,
              help="Don't skip _Unsorted_LEGACY_DO_NOT_TOUCH and ZZ_Private")
@click.option("--report", default="exif-heal-report.jsonl", type=click.Path(),
              help="JSONL report output path")
@click.option("--cache", "cache_path", default=".exif-heal-cache.db", type=click.Path(),
              help="SQLite cache path")
@click.option("--max-time-gap", default=21600, type=int,
              help="Max seconds between neighbors for interpolation (default: 6h)")
@click.option("--max-speed-kmh", default=1200.0, type=float,
              help="GPS-jump guard: max plausible travel speed (default: 1200)")
@click.option("--only-missing-time", is_flag=True,
              help="Only process files missing timestamps")
@click.option("--only-missing-gps", is_flag=True,
              help="Only process files missing GPS")
@click.option("--limit", type=int, default=None,
              help="Stop after N proposed changes")
@click.option("--allow-jumps", is_flag=True,
              help="Apply (downgraded) instead of skipping implausible-speed GPS")
@click.option("--allow-low-confidence", is_flag=True,
              help="Apply LOW confidence changes (default: MED+ only)")
@click.option("--min-confidence-time", default="med",
              help="Minimum confidence for time changes (high/med/low)")
@click.option("--min-confidence-gps", default="med",
              help="Minimum confidence for GPS changes (high/med/low)")
@click.option("--force", is_flag=True,
              help="Overwrite existing EXIF tags")
@click.option("--default-gps", default=None,
              help="Fallback GPS as 'lat,lon'")
@click.option("--gps-hints", default=None, type=click.Path(exists=True),
              help="Path to JSON file with time-period GPS defaults")
@click.option("--print-plan", is_flag=True,
              help="Print table of proposed changes")
@click.option("--jobs", "-j", type=int, default=None,
              help="Parallel exiftool reads (default: # CPU cores)")
@click.option("--verbose", "-v", is_flag=True,
              help="Enable debug logging")
def scan_cmd(root, ext, recursive, exclude_glob, no_default_excludes,
             report, cache_path, max_time_gap, max_speed_kmh,
             only_missing_time, only_missing_gps, limit,
             allow_jumps, allow_low_confidence, min_confidence_time,
             min_confidence_gps, force, default_gps, gps_hints,
             print_plan, jobs, verbose):
    """Scan files and propose EXIF changes."""
    _setup_logging(verbose)

    extensions = [e.strip().lower() for e in ext.split(",")]

    # Parse confidence thresholds
    min_time = parse_confidence(min_confidence_time)
    min_gps = parse_confidence(min_confidence_gps)

    if allow_low_confidence:
        min_time = Confidence.LOW
        min_gps = Confidence.LOW

    # Parse GPS defaults
    parsed_default_gps = _parse_gps(default_gps) if default_gps else None
    parsed_gps_hints = _load_gps_hints(gps_hints) if gps_hints else []

    config = ScanConfig(
        root=Path(root),
        extensions=extensions,
        recursive=recursive,
        max_time_gap=max_time_gap,
        max_speed_kmh=max_speed_kmh,
        only_missing_time=only_missing_time,
        only_missing_gps=only_missing_gps,
        limit=limit,
        allow_jumps=allow_jumps,
        allow_low_confidence=allow_low_confidence,
        min_confidence_time=min_time,
        min_confidence_gps=min_gps,
        force=force,
        default_gps=parsed_default_gps,
        gps_hints=parsed_gps_hints,
        exclude_globs=list(exclude_glob),
        no_default_excludes=no_default_excludes,
    )

    with MetadataCache(Path(cache_path)) as cache:
        with open(report, "w") as report_file:
            scan(config, cache, report_file, print_plan=print_plan, jobs=jobs)

    print(f"Report written to: {report}")
    print(f"Cache stored at:   {cache_path}")


@main.command()
@click.option("--root", required=True, type=click.Path(exists=True, file_okay=False),
              help="Root directory (for backup relative paths)")
@click.option("--cache", "cache_path", default=".exif-heal-cache.db",
              type=click.Path(exists=True),
              help="SQLite cache path (from previous scan)")
@click.option("--commit", is_flag=True,
              help="Actually write changes (default is dry-run)")
@click.option("--backup-dir", type=click.Path(), default=None,
              help="Copy originals here before modifying")
@click.option("--allow-low-confidence", is_flag=True,
              help="Apply LOW confidence changes")
@click.option("--min-confidence-time", default="med",
              help="Minimum confidence for time changes (high/med/low)")
@click.option("--min-confidence-gps", default="med",
              help="Minimum confidence for GPS changes (high/med/low)")
@click.option("--no-tag-provenance", is_flag=True,
              help="Disable writing XMP ExifHeal provenance tags")
@click.option("--no-xmp-mirror", is_flag=True,
              help="Disable mirroring EXIF tags to XMP equivalents")
@click.option("--limit", type=int, default=None,
              help="Apply changes to at most N files")
@click.option("--verbose", "-v", is_flag=True,
              help="Enable debug logging")
def apply_cmd(root, cache_path, commit, backup_dir,
              allow_low_confidence, min_confidence_time, min_confidence_gps,
              no_tag_provenance, no_xmp_mirror,
              limit, verbose):
    """Apply proposed EXIF changes from a previous scan."""
    _setup_logging(verbose)

    min_time = parse_confidence(min_confidence_time)
    min_gps = parse_confidence(min_confidence_gps)

    if allow_low_confidence:
        min_time = Confidence.LOW
        min_gps = Confidence.LOW

    with MetadataCache(Path(cache_path)) as cache:
        apply_changes(
            cache=cache,
            root=Path(root),
            commit=commit,
            backup_dir=Path(backup_dir) if backup_dir else None,
            min_confidence_time=min_time,
            min_confidence_gps=min_gps,
            tag_provenance=not no_tag_provenance,
            xmp_mirror=not no_xmp_mirror,
            limit=limit,
        )


@main.command()
@click.option("--root", required=True, type=click.Path(exists=True, file_okay=False),
              help="Root directory containing the batch to correct")
@click.option("--ext", default="dng,arw,jpg,jpeg",
              help="Comma-separated file extensions to shift")
@click.option("--recursive/--no-recursive", default=True,
              help="Recurse into subdirectories")
@click.option("--exclude-glob", multiple=True,
              help="Glob pattern for dirs/files to skip (repeatable)")
@click.option("--from-tz", default=None,
              help="Wrong IANA zone the clock was set to (e.g. America/Los_Angeles)")
@click.option("--to-tz", default=None,
              help="Correct IANA zone (e.g. Europe/Berlin). Use with --from-tz")
@click.option("--shift", "shift_offset", default=None,
              help="Fixed signed offset instead of zones, e.g. '+9:00' or '-5:30'")
@click.option("--make", "make_filter", default=None,
              help="Only shift files whose Make/Model contains this substring")
@click.option("--commit", is_flag=True,
              help="Actually write changes (default is dry-run)")
@click.option("--backup-dir", type=click.Path(), default=None,
              help="Copy originals here before modifying")
@click.option("--limit", type=int, default=None,
              help="Shift at most N files")
@click.option("--verbose", "-v", is_flag=True,
              help="Enable debug logging")
def shift_cmd(root, ext, recursive, exclude_glob, from_tz, to_tz, shift_offset,
              make_filter, commit, backup_dir, limit, verbose):
    """Correct wrong embedded timestamps by shifting them (timezone repair).

    Shifts DateTimeOriginal/CreateDate/ModifyDate on files that already have
    them — for batches where the camera clock was set to the wrong timezone.
    Specify EITHER --from-tz/--to-tz (DST-aware) OR a fixed --shift offset.
    GPS is never touched. Dry-run by default.
    """
    _setup_logging(verbose)

    extensions = [e.strip().lower() for e in ext.split(",")]

    using_zones = bool(from_tz or to_tz)
    if using_zones and shift_offset:
        raise click.UsageError("Use either --from-tz/--to-tz or --shift, not both.")
    if using_zones:
        if not (from_tz and to_tz):
            raise click.UsageError("--from-tz and --to-tz must be given together.")
        try:
            transform = make_zone_transform(from_tz, to_tz)
        except ValueError as e:
            raise click.BadParameter(str(e))
        desc = f"{from_tz} -> {to_tz}"
    elif shift_offset:
        try:
            transform = make_offset_transform(parse_offset(shift_offset))
        except ValueError as e:
            raise click.BadParameter(str(e))
        desc = f"offset {shift_offset}"
    else:
        raise click.UsageError(
            "Specify a correction: --from-tz/--to-tz or --shift '+H:MM'."
        )

    print(f"Shift mode: {desc}")
    run_shift(
        root=Path(root),
        extensions=extensions,
        transform=transform,
        recursive=recursive,
        exclude_globs=list(exclude_glob),
        make_filter=make_filter,
        commit=commit,
        backup_dir=Path(backup_dir) if backup_dir else None,
        limit=limit,
    )


# Register subcommands
main.add_command(scan_cmd, "scan")
main.add_command(apply_cmd, "apply")
main.add_command(shift_cmd, "shift")
