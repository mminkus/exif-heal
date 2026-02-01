"""Scan orchestrator: walk dirs, read metadata, infer, propose changes."""

from __future__ import annotations

import fnmatch
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Optional, TextIO

from . import exiftool
from .cache import MetadataCache
from .confidence import apply_confidence_gate
from .gps_infer import infer_gps
from .models import (
    Confidence,
    FileRecord,
    GPSCoord,
    ProposedChange,
    ScanConfig,
    ScanSummary,
    TimeSource,
)
from .report import print_plan_table, print_summary, write_report_line
from .time_infer import (
    detect_bulk_copy,
    establish_capture_time,
    infer_times,
)

logger = logging.getLogger(__name__)


def parse_exiftool_datetime(value) -> Optional[datetime]:
    """Parse an exiftool datetime string to a Python datetime.

    Handles formats like "2019:01:21 20:34:43" and "2019:01:21 20:34:43+00:00".
    Returns naive datetime (strips timezone).
    """
    if value is None or value == "" or value == "0000:00:00 00:00:00":
        return None

    s = str(value)
    # Strip timezone suffix if present
    if "+" in s and s.index("+") > 10:
        s = s[: s.index("+")]
    elif s.count("-") > 2:
        # Handle "2019:01:21 20:34:43-08:00" style
        parts = s.rsplit("-", 1)
        if len(parts) == 2 and ":" in parts[1] and len(parts[1]) <= 6:
            s = parts[0]

    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y:%m:%d"):
        try:
            return datetime.strptime(s.strip(), fmt)
        except ValueError:
            continue

    logger.debug("Could not parse datetime: %r", value)
    return None


def record_from_exiftool(raw: dict) -> FileRecord:
    """Convert a raw exiftool JSON dict to a FileRecord."""
    source_file = raw.get("SourceFile", "")
    path = Path(source_file).resolve()

    # Get file size from multiple possible keys
    file_size = exiftool.get_tag(raw, "FileSize", ["System", "File"])
    if file_size is None or isinstance(file_size, str):
        # Fall back to actual file size from filesystem
        try:
            file_size = path.stat().st_size if path.exists() else 0
        except OSError:
            file_size = 0
    file_size = int(file_size) if file_size else 0

    # Parse file mtime
    mtime_str = exiftool.get_tag(raw, "FileModifyDate", ["System"])
    file_mtime = parse_exiftool_datetime(mtime_str)
    if file_mtime is None:
        file_mtime = datetime.fromtimestamp(path.stat().st_mtime) if path.exists() else datetime.now()

    # Parse EXIF timestamps
    dto = parse_exiftool_datetime(
        exiftool.get_tag(raw, "DateTimeOriginal", ["ExifIFD", "IFD0", "XMP-exif"])
    )
    create = parse_exiftool_datetime(
        exiftool.get_tag(raw, "CreateDate", ["ExifIFD", "IFD0", "XMP-xmp"])
    )
    modify = parse_exiftool_datetime(
        exiftool.get_tag(raw, "ModifyDate", ["IFD0", "ExifIFD"])
    )
    xmp_created = parse_exiftool_datetime(
        exiftool.get_tag(raw, "DateCreated", ["XMP-xmp", "XMP-photoshop"])
    )

    # Parse GPS
    gps = None
    lat = exiftool.get_tag(raw, "GPSLatitude", ["GPS", "Composite", "XMP-exif"])
    lon = exiftool.get_tag(raw, "GPSLongitude", ["GPS", "Composite", "XMP-exif"])
    if lat is not None and lon is not None:
        try:
            gps = GPSCoord(lat=float(lat), lon=float(lon))
        except (ValueError, TypeError):
            pass

    # Camera info
    make = exiftool.get_tag(raw, "Make", ["IFD0"])
    model = exiftool.get_tag(raw, "Model", ["IFD0"])

    record = FileRecord(
        path=path,
        directory=str(path.parent),
        filename=path.name,
        extension=path.suffix.lstrip(".").lower(),
        file_mtime=file_mtime,
        file_size=int(file_size),
        datetime_original=dto,
        create_date=create,
        modify_date=modify,
        xmp_date_created=xmp_created,
        gps=gps,
        make=str(make) if make else None,
        model=str(model) if model else None,
    )

    # Establish capture time hierarchy
    establish_capture_time(record)

    return record


def should_exclude(path: str, exclude_globs: list[str]) -> bool:
    """Check if a path matches any exclude glob."""
    for pattern in exclude_globs:
        if fnmatch.fnmatch(path, pattern):
            return True
        # Also check if any parent matches
        if fnmatch.fnmatch(path + "/", pattern):
            return True
    return False


def walk_directories(
    root: Path,
    recursive: bool,
    exclude_globs: list[str],
) -> list[Path]:
    """Walk directories under root, respecting exclude globs."""
    dirs = []

    if recursive:
        for dirpath, dirnames, filenames in os.walk(root):
            dir_str = dirpath
            if should_exclude(dir_str, exclude_globs):
                dirnames.clear()  # Don't recurse into excluded dirs
                continue
            dirs.append(Path(dirpath))
    else:
        dirs.append(root)

    return dirs


def scan(
    config: ScanConfig,
    cache: MetadataCache,
    report_file: TextIO,
    print_plan: bool = False,
) -> ScanSummary:
    """Main scan pipeline.

    Walks directories, reads metadata, infers timestamps and GPS,
    applies guardrails, writes proposed changes to cache and JSONL.
    """
    summary = ScanSummary()
    run_id = cache.start_scan_run(str(config.root))
    all_changes: list[ProposedChange] = []
    change_count = 0

    # Walk directories
    directories = walk_directories(
        config.root, config.recursive, config.effective_excludes,
    )

    for directory in directories:
        summary.dirs_scanned += 1
        logger.info("Scanning %s", directory)

        # Read metadata via exiftool (batch JSON)
        raw_records = exiftool.batch_read_directory(directory, config.extensions)
        if not raw_records:
            continue

        # Convert to FileRecords
        records: list[FileRecord] = []
        for raw in raw_records:
            try:
                record = record_from_exiftool(raw)
                records.append(record)
            except Exception as e:
                logger.warning("Failed to parse record: %s", e)
                continue

        # Cache metadata
        for record in records:
            # Use actual filesystem mtime for cache freshness (not round-tripped
            # through exiftool datetime parsing, which strips timezone and
            # produces a different POSIX timestamp via naive .timestamp()).
            try:
                fs_mtime = record.path.stat().st_mtime
            except OSError:
                fs_mtime = record.file_mtime.timestamp()

            cache.upsert_file(
                path=str(record.path),
                directory=record.directory,
                filename=record.filename,
                extension=record.extension,
                mtime=fs_mtime,
                size=record.file_size,
                metadata={
                    "datetime_original": record.datetime_original.isoformat() if record.datetime_original else None,
                    "create_date": record.create_date.isoformat() if record.create_date else None,
                    "modify_date": record.modify_date.isoformat() if record.modify_date else None,
                    "gps_lat": record.gps.lat if record.gps else None,
                    "gps_lon": record.gps.lon if record.gps else None,
                    "make": record.make,
                    "model": record.model,
                    "capture_time": record.capture_time.isoformat() if record.capture_time else None,
                    "capture_time_source": record.capture_time_source.value if record.capture_time_source else None,
                },
            )

        summary.files_scanned += len(records)

        # Count missing
        missing_time = [r for r in records if not r.has_exif_time]
        missing_gps = [r for r in records if not r.has_gps]
        summary.files_missing_time += len(missing_time)
        summary.files_missing_gps += len(missing_gps)

        # Skip if nothing to do based on filters
        if config.only_missing_time and not missing_time:
            continue
        if config.only_missing_gps and not missing_gps:
            continue

        # Detect bulk-copy
        bulk_copied = detect_bulk_copy(records)
        if bulk_copied:
            summary.dirs_bulk_copied += 1
        cache.set_dir_flag(str(directory), bulk_copied)

        # Infer timestamps
        time_changes: list[ProposedChange] = []
        if not config.only_missing_gps:
            time_changes = infer_times(
                records,
                max_time_gap=config.max_time_gap,
                use_mtime=not bulk_copied,
                force=config.force,
            )

        # Build lookup for GPS merge
        time_changes_by_path = {str(c.path): c for c in time_changes}

        # Update FileRecords with just-inferred times so GPS inference can use them
        records_by_path = {str(r.path): r for r in records}
        for path_str, change in time_changes_by_path.items():
            if path_str in records_by_path and change.new_datetime_original:
                record = records_by_path[path_str]
                # Parse the inferred time back to datetime
                inferred_dt = parse_exiftool_datetime(change.new_datetime_original)
                if inferred_dt:
                    record.capture_time = inferred_dt
                    record.capture_time_source = change.time_source

        # Infer GPS
        gps_changes: list[ProposedChange] = []
        if not config.only_missing_time:
            gps_changes = infer_gps(
                records,
                max_time_gap=config.max_time_gap,
                max_distance_km=config.max_distance_km,
                allow_jumps=config.allow_jumps,
                default_gps=config.default_gps,
                gps_hints=config.gps_hints,
                existing_changes=time_changes_by_path,
                force=config.force,
            )

        # Merge all changes and deduplicate by path
        all_dir_changes = list(time_changes_by_path.values()) + gps_changes
        seen_paths = set()
        unique_changes = []
        for c in all_dir_changes:
            path_str = str(c.path)
            if path_str not in seen_paths and c.has_any_change:
                seen_paths.add(path_str)
                unique_changes.append(c)

        # Sort by best confidence (descending) so --limit prioritizes
        # high-confidence changes regardless of type (time vs GPS)
        conf_order = {Confidence.HIGH: 3, Confidence.MED: 2, Confidence.LOW: 1, Confidence.NONE: 0}
        unique_changes.sort(
            key=lambda c: max(conf_order.get(c.time_confidence, 0),
                              conf_order.get(c.gps_confidence, 0)),
            reverse=True,
        )

        # Apply confidence gating
        for change in unique_changes:
            apply_confidence_gate(
                change,
                min_confidence_time=config.min_confidence_time,
                min_confidence_gps=config.min_confidence_gps,
            )

        # Find matching records for report
        records_by_path = {str(r.path): r for r in records}

        # Write changes to cache and report, respecting --limit.
        # All change types (time + GPS) are computed above before the limit
        # is checked, so the limit doesn't bias toward one type.
        limit_hit = False
        for change in unique_changes:
            if config.limit and change_count >= config.limit:
                limit_hit = True
                break

            # Update summary counts
            if change.has_time_change:
                summary.files_proposed_time += 1
            if change.has_gps_change:
                summary.files_proposed_gps += 1
            if change.skipped:
                summary.files_skipped_guardrails += 1
            if change.gated_time or change.gated_gps:
                summary.files_gated += 1

            # Write to cache
            cache.set_proposed_change(
                path=str(change.path),
                proposed=_change_to_dict(change),
                confidence_time=change.time_confidence.value if change.has_time_change else None,
                confidence_gps=change.gps_confidence.value if change.has_gps_change else None,
            )

            # Write to JSONL report
            record = records_by_path.get(str(change.path))
            if record:
                write_report_line(report_file, record, change)

            all_changes.append(change)
            change_count += 1

        cache.commit()

        if limit_hit:
            logger.info("Reached limit of %d changes", config.limit)
            break

    # Finalize
    cache.finish_scan_run(
        run_id,
        file_count=summary.files_scanned,
        changes=change_count,
    )

    # Print summary
    print_summary(summary)

    if print_plan and all_changes:
        print_plan_table(all_changes, limit=50)

    return summary


def _change_to_dict(change: ProposedChange) -> dict:
    """Serialize a ProposedChange to a dict for cache storage."""
    d = {
        "path": str(change.path),
    }
    if change.has_time_change:
        d["time"] = {
            "datetime_original": change.new_datetime_original,
            "create_date": change.new_create_date,
            "modify_date": change.new_modify_date,
        }
    if change.has_gps_change:
        d["gps"] = {
            "lat": change.new_gps.lat,
            "lon": change.new_gps.lon,
        }
    d["provenance"] = {
        "time_source": change.time_source.value,
        "time_confidence": change.time_confidence.value,
        "gps_source": change.gps_source.value,
        "gps_confidence": change.gps_confidence.value,
    }
    d["skipped"] = change.skipped
    d["skip_reason"] = change.skip_reason
    d["gated_time"] = change.gated_time
    d["gated_gps"] = change.gated_gps
    d["gate_reason"] = change.gate_reason
    return d
