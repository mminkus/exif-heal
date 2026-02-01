"""ExifTool subprocess wrapper — JSON only, no text parsing."""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

EXIFTOOL = "exiftool"

# Tags we request (using -G1 group names for unambiguous JSON keys)
READ_TAGS = [
    "-ExifIFD:DateTimeOriginal",
    "-ExifIFD:CreateDate",
    "-IFD0:ModifyDate",
    "-GPS:GPSLatitude",
    "-GPS:GPSLongitude",
    "-XMP-xmp:DateCreated",
    "-System:FileModifyDate",
    "-System:FileName",
    "-System:Directory",
    "-System:FileSize",
    "-File:FileSize",
    "-IFD0:Make",
    "-IFD0:Model",
]


def batch_read_directory(
    directory: Path,
    extensions: list[str],
) -> list[dict]:
    """Read metadata for all matching files in a directory via JSON.

    Runs one exiftool invocation per directory (non-recursive).
    Returns list of raw exiftool JSON dicts, one per file.
    """
    cmd = [EXIFTOOL, "-j", "-n", "-G1", "-api", "IgnoreMinorErrors=1"]
    cmd.extend(READ_TAGS)
    for ext in extensions:
        cmd.extend(["-ext", ext])
    cmd.append(str(directory) + "/")

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except FileNotFoundError:
        raise RuntimeError(
            f"exiftool not found. Install it: https://exiftool.org/"
        )
    except subprocess.TimeoutExpired:
        logger.error("exiftool timed out reading %s", directory)
        return []

    # exiftool exits with 1 when no files match, 2 for errors
    if result.returncode == 1 and not result.stdout.strip():
        return []

    if result.stderr:
        for line in result.stderr.strip().split("\n"):
            if line.strip():
                logger.debug("exiftool stderr: %s", line.strip())

    if not result.stdout.strip():
        return []

    try:
        records = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        logger.error("Failed to parse exiftool JSON for %s: %s", directory, e)
        return []

    return records


def batch_read_files(
    files: list[Path],
) -> list[dict]:
    """Read metadata for a specific list of files via JSON.

    Useful for targeted re-reads after apply.
    """
    if not files:
        return []

    cmd = [EXIFTOOL, "-j", "-n", "-G1", "-api", "IgnoreMinorErrors=1"]
    cmd.extend(READ_TAGS)
    cmd.extend(str(f) for f in files)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except FileNotFoundError:
        raise RuntimeError("exiftool not found.")
    except subprocess.TimeoutExpired:
        logger.error("exiftool timed out reading %d files", len(files))
        return []

    if not result.stdout.strip():
        return []

    try:
        records = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        logger.error("Failed to parse exiftool JSON: %s", e)
        return []

    return records


def get_tag(record: dict, tag_name: str, groups: Optional[list[str]] = None) -> Optional[str]:
    """Extract a tag value from an exiftool JSON record.

    Checks multiple group prefixes since different file types use different groups.
    For example, DateTimeOriginal could be "ExifIFD:DateTimeOriginal" or "IFD0:DateTimeOriginal".
    """
    if groups:
        for group in groups:
            key = f"{group}:{tag_name}"
            if key in record:
                val = record[key]
                if val is not None and val != "" and val != "0000:00:00 00:00:00":
                    return val
    # Also check without group prefix (some formats)
    if tag_name in record:
        val = record[tag_name]
        if val is not None and val != "" and val != "0000:00:00 00:00:00":
            return val
    return None


def generate_argfile(
    changes: list[dict],
    tag_provenance: bool = True,
    xmp_mirror: bool = True,
) -> str:
    """Generate exiftool argfile content.

    Each entry in changes is a dict with:
      - path: str (file path)
      - time: optional dict with datetime_original, create_date, modify_date
      - gps: optional dict with lat, lon
      - provenance: optional dict with time_source, time_confidence, gps_source, gps_confidence

    Format: one block per file, separated by -execute directives.
    """
    lines = []
    for change in changes:
        file_path = change["path"]

        if "time" in change:
            t = change["time"]
            if t.get("datetime_original"):
                lines.append(f"-DateTimeOriginal={t['datetime_original']}")
            if t.get("create_date"):
                lines.append(f"-CreateDate={t['create_date']}")
            if t.get("modify_date"):
                lines.append(f"-ModifyDate={t['modify_date']}")
            if xmp_mirror:
                if t.get("datetime_original"):
                    lines.append(f"-XMP-xmp:DateCreated={t['datetime_original']}")
                    lines.append(f"-XMP-photoshop:DateCreated={t['datetime_original']}")

        if "gps" in change:
            g = change["gps"]
            lines.append(f"-GPSLatitude={g['lat']}")
            lines.append(f"-GPSLongitude={g['lon']}")
            if xmp_mirror:
                lines.append(f"-XMP-exif:GPSLatitude={g['lat']}")
                lines.append(f"-XMP-exif:GPSLongitude={g['lon']}")

        if tag_provenance and "provenance" in change:
            p = change["provenance"]
            if p.get("time_source"):
                lines.append(f"-XMP-xmp:ExifHealTimeSource={p['time_source']}")
            if p.get("time_confidence"):
                lines.append(f"-XMP-xmp:ExifHealTimeConfidence={p['time_confidence']}")
            if p.get("gps_source"):
                lines.append(f"-XMP-xmp:ExifHealGPSSource={p['gps_source']}")
            if p.get("gps_confidence"):
                lines.append(f"-XMP-xmp:ExifHealGPSConfidence={p['gps_confidence']}")

        lines.append(file_path)
        lines.append("-execute")

    return "\n".join(lines)


def write_via_argfile(
    argfile_path: Path,
    expected_paths: list[str],
) -> tuple[list[str], int, str]:
    """Execute exiftool write using an argfile.

    Runs: exiftool -overwrite_original_in_place -P -@ <argfile>

    Returns (successfully_written_paths, error_count, stderr_output).
    The successfully_written_paths list contains paths that were updated.
    """
    cmd = [
        EXIFTOOL,
        "-overwrite_original_in_place",
        "-P",
        "-@",
        str(argfile_path),
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=600,
        )
    except FileNotFoundError:
        raise RuntimeError("exiftool not found.")
    except subprocess.TimeoutExpired:
        logger.error("exiftool timed out during write")
        return [], 0, "timeout"

    stderr = result.stderr or ""
    stdout = result.stdout or ""

    # Parse output to track which -execute batches succeeded.
    # Each file is in its own -execute batch, so we can map results 1:1.
    #
    # Per-batch output is one of:
    #   "N image files updated"   → success if N > 0, failure if N == 0
    #   "Nothing to do."          → no valid tags, counts as failure
    #
    # A summary line "N files weren't updated due to errors" may also appear
    # but is NOT a per-batch result and must be excluded from batch tracking.
    success_per_batch = []
    total_errors = 0
    for line in stdout.split("\n"):
        line = line.strip()
        lower = line.lower()
        if "files weren't updated due to errors" in lower:
            try:
                total_errors = int(line.split()[0])
            except (ValueError, IndexError):
                pass
        elif "image files updated" in lower:
            try:
                count = int(line.split()[0])
                success_per_batch.append(count > 0)
            except (ValueError, IndexError):
                pass
        elif lower == "nothing to do.":
            success_per_batch.append(False)

    # Map success flags back to file paths
    successfully_written = []
    for idx, path in enumerate(expected_paths):
        if idx < len(success_per_batch) and success_per_batch[idx]:
            successfully_written.append(path)

    return successfully_written, total_errors, stderr
