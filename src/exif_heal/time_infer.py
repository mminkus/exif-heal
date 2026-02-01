"""Timestamp inference: filename parsing, camera-session neighbor selection, interpolation."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Optional

from .models import Confidence, FileRecord, ProposedChange, TimeSource

logger = logging.getLogger(__name__)

# Filename patterns: (regex, group_names, has_time_component)
FILENAME_PATTERNS = [
    # received_YYYYMMDD_<random>.jpeg — Messenger
    (
        re.compile(r"received_(\d{4})(\d{2})(\d{2})_"),
        ("Y", "m", "d"),
        False,
    ),
    # IMG_YYYYMMDD_HHMMSS — standard Android/camera
    (
        re.compile(r"(?:IMG|VID|PXL)_(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})"),
        ("Y", "m", "d", "H", "M", "S"),
        True,
    ),
    # YYYYMMDD_HHMMSS — bare timestamp
    (
        re.compile(r"^(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})"),
        ("Y", "m", "d", "H", "M", "S"),
        True,
    ),
    # IMG-YYYYMMDD-WA0001 — WhatsApp
    (
        re.compile(r"IMG-(\d{4})(\d{2})(\d{2})-WA"),
        ("Y", "m", "d"),
        False,
    ),
    # Screenshot_YYYYMMDD-HHMMSS
    (
        re.compile(r"Screenshot_(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})"),
        ("Y", "m", "d", "H", "M", "S"),
        True,
    ),
    # YYYY-MM-DD HH.MM.SS or YYYY-MM-DD_HH.MM.SS
    (
        re.compile(r"(\d{4})-(\d{2})-(\d{2})[_ ](\d{2})\.(\d{2})\.(\d{2})"),
        ("Y", "m", "d", "H", "M", "S"),
        True,
    ),
]


def parse_filename_time(filename: str) -> tuple[Optional[datetime], bool]:
    """Try to extract a timestamp from a filename.

    Returns (datetime, has_time_component).
    has_time_component=False means only date was parsed (time is midnight).
    Returns (None, False) if no pattern matches.
    """
    for pattern, groups, has_time in FILENAME_PATTERNS:
        m = pattern.search(filename)
        if not m:
            continue

        parts = {}
        for i, name in enumerate(groups):
            parts[name] = int(m.group(i + 1))

        # Validate
        year = parts.get("Y", 0)
        month = parts.get("m", 0)
        day = parts.get("d", 0)
        hour = parts.get("H", 0)
        minute = parts.get("M", 0)
        second = parts.get("S", 0)

        if year < 1990 or year > 2030:
            continue
        if month < 1 or month > 12:
            continue
        if day < 1 or day > 31:
            continue
        if hour > 23 or minute > 59 or second > 59:
            continue

        try:
            dt = datetime(year, month, day, hour, minute, second)
            return dt, has_time
        except ValueError:
            continue

    return None, False


def establish_capture_time(record: FileRecord) -> FileRecord:
    """Walk the priority chain to set capture_time on a FileRecord.

    Mutates the record in-place and returns it.
    """
    # Priority 1: DateTimeOriginal
    if record.datetime_original:
        record.capture_time = record.datetime_original
        record.capture_time_source = TimeSource.EXIF_DTO
        return record

    # Priority 2: CreateDate
    if record.create_date:
        record.capture_time = record.create_date
        record.capture_time_source = TimeSource.EXIF_CREATE
        return record

    # Priority 3: ModifyDate
    if record.modify_date:
        record.capture_time = record.modify_date
        record.capture_time_source = TimeSource.EXIF_MODIFY
        return record

    # Priority 4: XMP:DateCreated
    if record.xmp_date_created:
        record.capture_time = record.xmp_date_created
        record.capture_time_source = TimeSource.XMP_CREATED
        return record

    # Priority 5: Filename
    ft, has_time = parse_filename_time(record.filename)
    if ft:
        record.filename_time = ft
        record.filename_time_has_time = has_time
        record.capture_time = ft
        record.capture_time_source = TimeSource.FILENAME
        return record

    # Priority 6: mtime (set as filename_time placeholder, not capture_time yet)
    # mtime is only used if the directory is not bulk-copied
    # The caller (scanner) decides whether to use mtime based on bulk-copy detection
    ft, has_time = parse_filename_time(record.filename)
    record.filename_time = ft
    record.filename_time_has_time = has_time
    return record


def sort_key(record: FileRecord, use_mtime: bool = True) -> tuple:
    """Generate a sort key for ordering files within a directory.

    Returns (datetime, filename) tuple for stable sorting.
    """
    if record.capture_time:
        return (record.capture_time, record.filename)
    if record.filename_time:
        return (record.filename_time, record.filename)
    if use_mtime:
        return (record.file_mtime, record.filename)
    # No time info at all — sort by filename only, but push to the end
    return (datetime.max, record.filename)


def detect_bulk_copy(records: list[FileRecord]) -> bool:
    """Detect if a directory was bulk-copied (>80% same mtime within 60s).

    Returns True if the directory appears bulk-copied.
    """
    if len(records) < 3:
        return False

    # Count mtime clusters
    mtimes = sorted(r.file_mtime for r in records)
    clusters: list[list[datetime]] = []
    for mt in mtimes:
        if clusters and abs((mt - clusters[-1][-1]).total_seconds()) <= 60:
            clusters[-1].append(mt)
        else:
            clusters.append([mt])

    # Find largest cluster
    largest = max(len(c) for c in clusters)
    ratio = largest / len(records)

    if ratio > 0.8:
        logger.info(
            "Directory bulk-copy detected: %d/%d files (%.0f%%) share mtime",
            largest, len(records), ratio * 100,
        )
        return True
    return False


def find_time_neighbors(
    target_idx: int,
    files: list[FileRecord],
    max_gap_seconds: int,
    prefer_camera_key: Optional[str] = None,
) -> tuple[Optional[FileRecord], Optional[FileRecord]]:
    """Find nearest anchor files before and after the target.

    An "anchor" has capture_time_source in {EXIF_DTO, EXIF_CREATE, EXIF_MODIFY, XMP_CREATED}
    or FILENAME with full time.

    When prefer_camera_key is set, first tries to find neighbors with that camera key.
    Falls back to any anchor if no camera-session matches found.
    """
    anchor_sources = {
        TimeSource.EXIF_DTO, TimeSource.EXIF_CREATE,
        TimeSource.EXIF_MODIFY, TimeSource.XMP_CREATED,
    }

    def is_anchor(r: FileRecord) -> bool:
        if r.capture_time_source in anchor_sources:
            return True
        if r.capture_time_source == TimeSource.FILENAME and r.filename_time_has_time:
            return True
        return False

    def is_within_gap(anchor: FileRecord, target: FileRecord) -> bool:
        a_time = anchor.capture_time or anchor.filename_time or anchor.file_mtime
        t_time = target.capture_time or target.filename_time or target.file_mtime
        return abs((a_time - t_time).total_seconds()) <= max_gap_seconds

    target = files[target_idx]

    # Try camera-session neighbors first
    for camera_filter in ([prefer_camera_key, None] if prefer_camera_key else [None]):
        before = None
        after = None

        # Walk left
        for i in range(target_idx - 1, -1, -1):
            f = files[i]
            if not is_anchor(f):
                continue
            if camera_filter and f.camera_key != camera_filter:
                continue
            if is_within_gap(f, target):
                before = f
            break

        # Walk right
        for i in range(target_idx + 1, len(files)):
            f = files[i]
            if not is_anchor(f):
                continue
            if camera_filter and f.camera_key != camera_filter:
                continue
            if is_within_gap(f, target):
                after = f
            break

        if before is not None or after is not None:
            return before, after

    return None, None


def interpolate_time(
    target_idx: int,
    files: list[FileRecord],
    before: Optional[FileRecord],
    after: Optional[FileRecord],
) -> tuple[datetime, Confidence, TimeSource, str]:
    """Compute interpolated time for the target file.

    Returns (inferred_time, confidence, source, reason).
    """
    target = files[target_idx]

    if before is not None and after is not None:
        # Both neighbors — linear interpolation by position
        before_idx = files.index(before)
        after_idx = files.index(after)
        n = after_idx - before_idx  # total span
        pos = target_idx - before_idx  # position within span

        if n > 0 and before.capture_time and after.capture_time:
            fraction = pos / n
            delta = after.capture_time - before.capture_time
            inferred = before.capture_time + timedelta(seconds=delta.total_seconds() * fraction)

            # Confidence: HIGH if same camera on both sides
            same_camera = (
                before.camera_key is not None
                and before.camera_key == after.camera_key
            )
            confidence = Confidence.HIGH if same_camera else Confidence.MED

            reason = (
                f"interpolated between {before.filename} and {after.filename} "
                f"(pos {pos}/{n}, {'same' if same_camera else 'diff'} camera)"
            )
            return inferred, confidence, TimeSource.NEIGHBOR_INTERP, reason

    if before is not None and before.capture_time:
        # Only before neighbor — copy + offset
        offset = target_idx - files.index(before)
        inferred = before.capture_time + timedelta(seconds=offset)
        reason = f"copied from {before.filename} +{offset}s"
        return inferred, Confidence.MED, TimeSource.NEIGHBOR_COPY, reason

    if after is not None and after.capture_time:
        # Only after neighbor — copy - offset
        offset = files.index(after) - target_idx
        inferred = after.capture_time - timedelta(seconds=offset)
        reason = f"copied from {after.filename} -{offset}s"
        return inferred, Confidence.MED, TimeSource.NEIGHBOR_COPY, reason

    # No neighbors — fall back to filename time or mtime
    if target.filename_time:
        confidence = Confidence.MED if target.filename_time_has_time else Confidence.LOW
        source_detail = "full" if target.filename_time_has_time else "date_only"
        reason = f"filename timestamp ({source_detail})"
        return target.filename_time, confidence, TimeSource.FILENAME, reason

    # mtime fallback
    reason = "file modification time (last resort)"
    return target.file_mtime, Confidence.LOW, TimeSource.MTIME, reason


def infer_times(
    files: list[FileRecord],
    max_time_gap: int,
    use_mtime: bool = True,
    force: bool = False,
) -> list[ProposedChange]:
    """Infer timestamps for files missing EXIF time data.

    Args:
        files: All FileRecords in one directory, with capture_time established.
        max_time_gap: Maximum seconds between neighbors for interpolation.
        use_mtime: If False (bulk-copied dir), don't use mtime as evidence.
        force: If True, process files even if they already have EXIF time.

    Returns list of ProposedChanges for files that were missing timestamps.
    """
    # Sort files
    sorted_files = sorted(files, key=lambda r: sort_key(r, use_mtime=use_mtime))

    changes = []
    for idx, record in enumerate(sorted_files):
        # Skip files that already have EXIF time (unless --force)
        if record.has_exif_time and not force:
            continue

        # Find neighbors
        before, after = find_time_neighbors(
            idx, sorted_files, max_time_gap,
            prefer_camera_key=record.camera_key,
        )

        # Interpolate
        inferred_time, confidence, source, reason = interpolate_time(
            idx, sorted_files, before, after,
        )

        # If mtime not allowed and source is MTIME, skip
        if not use_mtime and source == TimeSource.MTIME:
            logger.debug("Skipping mtime fallback for %s (bulk-copied dir)", record.filename)
            continue

        # Format for exiftool
        time_str = inferred_time.strftime("%Y:%m:%d %H:%M:%S")

        change = ProposedChange(
            path=record.path,
            new_datetime_original=time_str,
            new_create_date=time_str,
            time_confidence=confidence,
            time_source=source,
            reason_time=reason,
        )

        # Only set ModifyDate when confidence >= MED and source != mtime
        if confidence >= Confidence.MED and source != TimeSource.MTIME:
            change.new_modify_date = time_str

        # Track neighbors used
        if before:
            change.neighbors_time.append(str(before.path))
        if after:
            change.neighbors_time.append(str(after.path))

        # Check mtime drift guardrail
        drift = abs((inferred_time - record.file_mtime).total_seconds())
        drift_years = drift / (365.25 * 86400)
        change.time_mtime_drift_years = drift_years
        if drift_years > 2.0:
            logger.warning(
                "Large time drift for %s: %.1f years (inferred=%s, mtime=%s)",
                record.filename, drift_years, inferred_time, record.file_mtime,
            )
            change.time_confidence = Confidence.LOW
            change.reason_time += f" [DRIFT: {drift_years:.1f}yr from mtime]"
            # Unset ModifyDate if confidence downgraded to LOW
            change.new_modify_date = None

        changes.append(change)

    return changes
