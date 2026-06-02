"""Timezone correction: bulk-shift existing timestamps for targeted files.

This is distinct from the gap-filling inference in `scanner`/`time_infer`. It
repairs files whose embedded times are *present but wrong* — e.g. a camera left
on the wrong timezone — by shifting DateTimeOriginal/CreateDate/ModifyDate by a
fixed offset or by a `from_tz -> to_tz` (DST-aware) conversion.

It reuses the apply machinery: argfile generation, re-read verification, and
backups. It does not touch GPS and does not use the cache.
"""

from __future__ import annotations

import logging
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import exiftool
from .backup import backup_file
from .scanner import record_from_exiftool, walk_directories

logger = logging.getLogger(__name__)

_OFFSET_RE = re.compile(r"^([+-])(\d{1,2})(?::(\d{2}))?(?::(\d{2}))?$")


@dataclass
class ShiftSummary:
    """Summary of a shift run."""

    files_scanned: int = 0
    files_shifted: int = 0
    files_skipped_no_time: int = 0
    files_skipped_filter: int = 0
    backed_up: int = 0
    written: int = 0
    errors: int = 0
    dry_run: bool = True


def parse_offset(value: str) -> timedelta:
    """Parse a signed offset like '+3', '+3:00', '-5:30', '+9:00:00'.

    Returns a timedelta. Raises ValueError on bad input.
    """
    m = _OFFSET_RE.match(value.strip())
    if not m:
        raise ValueError(
            f"Invalid offset {value!r}; expected like '+3:00', '-5:30', '+9'"
        )
    sign = 1 if m.group(1) == "+" else -1
    hours = int(m.group(2))
    minutes = int(m.group(3)) if m.group(3) else 0
    seconds = int(m.group(4)) if m.group(4) else 0
    return sign * timedelta(hours=hours, minutes=minutes, seconds=seconds)


def make_offset_transform(delta: timedelta) -> Callable[[datetime], datetime]:
    """Build a transform that adds a fixed delta to a naive datetime."""
    return lambda dt: dt + delta


def make_zone_transform(from_tz: str, to_tz: str) -> Callable[[datetime], datetime]:
    """Build a transform that reinterprets a naive time from `from_tz` and
    re-expresses the same instant in `to_tz` (DST-aware), returning naive.

    Raises ValueError if a zone name is unknown.
    """
    try:
        fz = ZoneInfo(from_tz)
        tz = ZoneInfo(to_tz)
    except ZoneInfoNotFoundError as e:
        raise ValueError(
            f"Unknown timezone {e}. Install the 'tzdata' package or use a valid "
            f"IANA name (e.g. America/Los_Angeles)."
        )

    def transform(dt: datetime) -> datetime:
        return dt.replace(tzinfo=fz).astimezone(tz).replace(tzinfo=None)

    return transform


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y:%m:%d %H:%M:%S")


def run_shift(
    root: Path,
    extensions: list[str],
    transform: Callable[[datetime], datetime],
    *,
    recursive: bool = True,
    exclude_globs: Optional[list[str]] = None,
    make_filter: Optional[str] = None,
    commit: bool = False,
    backup_dir: Optional[Path] = None,
    limit: Optional[int] = None,
) -> ShiftSummary:
    """Shift existing timestamps for files under `root` matching `extensions`.

    `transform` maps an existing naive datetime to its corrected value. Only the
    date fields that are actually present on a file are shifted; GPS is untouched.
    Dry-run by default; pass commit=True to write.
    """
    summary = ShiftSummary(dry_run=not commit)
    exclude_globs = exclude_globs or []
    make_filter_lower = make_filter.lower() if make_filter else None

    directories = walk_directories(root, recursive, exclude_globs)

    changes: list[dict] = []
    previews: list[str] = []
    for directory in directories:
        raw_records = exiftool.batch_read_directory(directory, extensions)
        for raw in raw_records:
            try:
                record = record_from_exiftool(raw)
            except Exception as e:  # pragma: no cover - defensive
                logger.warning("Failed to parse record: %s", e)
                continue

            summary.files_scanned += 1

            if make_filter_lower is not None:
                make = (record.make or "").lower()
                model = (record.model or "").lower()
                if make_filter_lower not in make and make_filter_lower not in model:
                    summary.files_skipped_filter += 1
                    continue

            present = {
                "datetime_original": record.datetime_original,
                "create_date": record.create_date,
                "modify_date": record.modify_date,
            }
            if not any(present.values()):
                summary.files_skipped_no_time += 1
                continue

            time_block: dict[str, Optional[str]] = {}
            for field, dt in present.items():
                time_block[field] = _fmt(transform(dt)) if dt else None

            changes.append({"path": str(record.path), "time": time_block})
            # Preview line: show the canonical capture time before -> after
            canon = (
                record.datetime_original
                or record.create_date
                or record.modify_date
            )
            previews.append(
                f"    {record.path}: {_fmt(canon)} -> {_fmt(transform(canon))}"
            )

    if limit:
        changes = changes[:limit]
        previews = previews[:limit]

    if not changes:
        print("No files with timestamps to shift.")
        return summary

    print(f"\nFiles to shift: {len(changes)}")
    if summary.files_skipped_no_time:
        print(f"  Skipped (no timestamp):   {summary.files_skipped_no_time}")
    if summary.files_skipped_filter:
        print(f"  Skipped (make filter):    {summary.files_skipped_filter}")

    if not commit:
        print("\n  DRY RUN — no files will be modified. Use --commit to write.\n")
        for line in previews[:20]:
            print(line)
        if len(previews) > 20:
            print(f"    ... and {len(previews) - 20} more")
        print()
        summary.written = len(changes)  # would-be count
        return summary

    # Backup originals
    if backup_dir:
        print(f"\nBacking up originals to {backup_dir}...")
        for change in changes:
            source = Path(change["path"])
            if source.exists():
                try:
                    backup_file(source, root, Path(backup_dir))
                    summary.backed_up += 1
                except Exception as e:
                    logger.error("Failed to backup %s: %s", source, e)
                    summary.errors += 1
        print(f"  Backed up {summary.backed_up} files.")

    # Generate argfile and write. No provenance tags (this is a correction, not
    # an inference); mirror to XMP so XMP:DateCreated stays consistent.
    argfile_content = exiftool.generate_argfile(
        changes, tag_provenance=False, xmp_mirror=True
    )
    if not argfile_content.strip():
        print("Nothing to write.")
        return summary

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".args", prefix="exif-heal-shift-", delete=False
    ) as f:
        f.write(argfile_content)
        argfile_path = Path(f.name)

    try:
        print(f"\nRunning exiftool on {len(changes)} file(s)...")
        written, errors, stderr = exiftool.write_via_argfile(argfile_path, changes)
        summary.written = len(written)
        summary.errors = errors
        if stderr:
            for line in stderr.strip().split("\n"):
                if line.strip():
                    logger.warning("exiftool: %s", line.strip())
        print(f"  Updated: {len(written)}")
        if errors:
            print(f"  Errors:  {errors}")
        summary.files_shifted = len(written)
    finally:
        try:
            argfile_path.unlink()
        except OSError:
            pass

    return summary
