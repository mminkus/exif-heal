"""ExifTool subprocess wrapper — JSON only, no text parsing."""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Optional

from .models import is_quicktime_video

logger = logging.getLogger(__name__)

EXIFTOOL = "exiftool"

# Files per exiftool read call. Reading thousands of large RAWs in a single
# call can exceed the per-call timeout and silently drop a whole directory, so
# directory reads are chunked. Each chunk is well within the timeout.
READ_CHUNK_SIZE = 1000

# Config defining the custom XMP-exifheal:* provenance namespace. Required
# (via -config) to WRITE those tags; reads work without it. Empty list if the
# bundled config is somehow missing, in which case provenance is skipped.
CONFIG_PATH = Path(__file__).parent / "exiftool_config"
HAS_CONFIG = CONFIG_PATH.exists()
CONFIG_ARGS = ["-config", str(CONFIG_PATH)] if HAS_CONFIG else []

# Tags we request (using -G1 group names for unambiguous JSON keys)
READ_TAGS = [
    "-ExifIFD:DateTimeOriginal",
    "-ExifIFD:CreateDate",
    "-IFD0:ModifyDate",
    "-GPS:GPSLatitude",
    "-GPS:GPSLongitude",
    "-GPS:GPSLatitudeRef",
    "-GPS:GPSLongitudeRef",
    # Composite gives the SIGNED decimal coordinate (negative for S/W);
    # the raw GPS:GPSLatitude is only the unsigned magnitude.
    "-Composite:GPSLatitude",
    "-Composite:GPSLongitude",
    "-XMP-xmp:DateCreated",
    "-System:FileModifyDate",
    "-System:FileName",
    "-System:Directory",
    "-System:FileSize",
    "-File:FileSize",
    "-IFD0:Make",
    "-IFD0:Model",
    # Video (QuickTime-family) time + GPS. The base GPSCoordinates tags are
    # requested so exiftool can derive the signed Composite:GPSLatitude/Longitude.
    "-QuickTime:CreateDate",
    "-QuickTime:ModifyDate",
    "-Keys:CreationDate",
    "-Keys:GPSCoordinates",
    "-QuickTime:GPSCoordinates",
    "-UserData:GPSCoordinates",
    # exif-heal provenance (read back fine without -config).
    "-XMP-exifheal:TimeSource",
    "-XMP-exifheal:TimeConfidence",
    "-XMP-exifheal:GPSSource",
    "-XMP-exifheal:GPSConfidence",
]


def _list_dir_files(directory: Path, extensions: list[str]) -> list[Path]:
    """Non-recursively list files in `directory` matching `extensions`
    (case-insensitive), sorted for determinism."""
    exts = {e.lstrip(".").lower() for e in extensions}
    files: list[Path] = []
    try:
        with os.scandir(directory) as it:
            for entry in it:
                try:
                    if not entry.is_file():
                        continue
                except OSError:
                    continue
                name = entry.name
                ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
                if ext in exts:
                    files.append(Path(entry.path))
    except OSError as e:
        logger.error("Cannot list directory %s: %s", directory, e)
    return sorted(files)


def batch_read_directory(
    directory: Path,
    extensions: list[str],
) -> list[dict]:
    """Read metadata for all matching files in a directory via JSON.

    Non-recursive. Reads in chunks of READ_CHUNK_SIZE files so a very large
    directory can't exceed the per-call timeout and silently drop every file.
    Returns list of raw exiftool JSON dicts, one per file.
    """
    files = _list_dir_files(directory, extensions)
    if not files:
        return []

    records: list[dict] = []
    for i in range(0, len(files), READ_CHUNK_SIZE):
        records.extend(batch_read_files(files[i:i + READ_CHUNK_SIZE]))

    if len(records) < len(files):
        logger.warning(
            "Read %d of %d files in %s — %d not returned (unreadable or timed out)",
            len(records), len(files), directory, len(files) - len(records),
        )
    return records


def batch_read_files(
    files: list[Path],
) -> list[dict]:
    """Read metadata for a specific list of files via JSON.

    Useful for targeted re-reads after apply.
    """
    if not files:
        return []

    cmd = [EXIFTOOL, *CONFIG_ARGS, "-j", "-n", "-G1", "-api", "IgnoreMinorErrors=1"]
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


def read_gps(record: dict) -> Optional[tuple[float, float]]:
    """Read a SIGNED (lat, lon) pair from an exiftool record.

    exiftool's raw ``GPS:GPSLatitude`` (with ``-n``) is the *unsigned*
    magnitude; the sign lives in ``GPSLatitudeRef`` (N/S) and
    ``GPSLongitudeRef`` (E/W). ``Composite:GPSLatitude`` already folds the
    ref into a signed decimal, so prefer it and fall back to magnitude+ref.

    Returns None if no usable coordinate is present.
    """
    # Preferred: Composite is already signed.
    lat = get_tag(record, "GPSLatitude", ["Composite"])
    lon = get_tag(record, "GPSLongitude", ["Composite"])
    if lat is not None and lon is not None:
        try:
            return float(lat), float(lon)
        except (ValueError, TypeError):
            pass

    # Fallback: magnitude + hemisphere ref.
    lat = get_tag(record, "GPSLatitude", ["GPS", "XMP-exif"])
    lon = get_tag(record, "GPSLongitude", ["GPS", "XMP-exif"])
    if lat is None or lon is None:
        return None
    try:
        flat, flon = float(lat), float(lon)
    except (ValueError, TypeError):
        return None

    lat_ref = get_tag(record, "GPSLatitudeRef", ["GPS"])
    lon_ref = get_tag(record, "GPSLongitudeRef", ["GPS"])
    if lat_ref and str(lat_ref).strip().upper().startswith("S"):
        flat = -abs(flat)
    if lon_ref and str(lon_ref).strip().upper().startswith("W"):
        flon = -abs(flon)
    return flat, flon


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
        is_video = is_quicktime_video(Path(file_path).suffix)

        if "time" in change:
            t = change["time"]
            if is_video:
                # QuickTime container: dates live under QuickTime/Track/Media,
                # not ExifIFD. Map our capture time onto the create-style tags
                # and modify time onto the modify-style tags.
                capture = t.get("datetime_original") or t.get("create_date")
                if capture:
                    for tag in ("QuickTime:CreateDate", "TrackCreateDate",
                                "MediaCreateDate"):
                        lines.append(f"-{tag}={capture}")
                if t.get("modify_date"):
                    for tag in ("QuickTime:ModifyDate", "TrackModifyDate",
                                "MediaModifyDate"):
                        lines.append(f"-{tag}={t['modify_date']}")
            else:
                if t.get("datetime_original"):
                    lines.append(f"-DateTimeOriginal={t['datetime_original']}")
                if t.get("create_date"):
                    lines.append(f"-CreateDate={t['create_date']}")
                if t.get("modify_date"):
                    lines.append(f"-ModifyDate={t['modify_date']}")
                if xmp_mirror and t.get("datetime_original"):
                    # XMP-xmp:CreateDate is the writable xmp tag (DateCreated is
                    # not defined in the xmp namespace); photoshop:DateCreated
                    # is the other widely-read mirror.
                    lines.append(f"-XMP-xmp:CreateDate={t['datetime_original']}")
                    lines.append(f"-XMP-photoshop:DateCreated={t['datetime_original']}")

        if "gps" in change:
            g = change["gps"]
            if is_video:
                # QuickTime stores GPS as an ISO-6709-ish "lat lon" string;
                # exiftool derives the signed Composite:GPSLatitude from it.
                lines.append(f"-Keys:GPSCoordinates={g['lat']} {g['lon']}")
            else:
                # The "*" suffix tells exiftool to also write the matching
                # GPSLatitudeRef/GPSLongitudeRef. Without it, a signed decimal
                # is stored as an unsigned magnitude and the hemisphere is lost.
                lines.append(f"-GPSLatitude*={g['lat']}")
                lines.append(f"-GPSLongitude*={g['lon']}")
                if xmp_mirror:
                    lines.append(f"-XMP-exif:GPSLatitude={g['lat']}")
                    lines.append(f"-XMP-exif:GPSLongitude={g['lon']}")

        if tag_provenance and "provenance" in change:
            # Custom XMP-exifheal namespace (writable only with -config, which
            # write_via_argfile passes). Stock XMP-xmp:ExifHeal* is "not
            # defined" and silently dropped.
            p = change["provenance"]
            if p.get("time_source"):
                lines.append(f"-XMP-exifheal:TimeSource={p['time_source']}")
            if p.get("time_confidence"):
                lines.append(f"-XMP-exifheal:TimeConfidence={p['time_confidence']}")
            if p.get("gps_source"):
                lines.append(f"-XMP-exifheal:GPSSource={p['gps_source']}")
            if p.get("gps_confidence"):
                lines.append(f"-XMP-exifheal:GPSConfidence={p['gps_confidence']}")

        lines.append(file_path)
        lines.append("-execute")

    return "\n".join(lines)


def _change_landed(change: dict, record: dict) -> bool:
    """Check whether the intended values in `change` are present in `record`.

    Compares the re-read tags against what we meant to write. A change counts
    as landed only if every intended component (time and/or GPS) is present and
    matches.
    """
    # Lazy import to avoid a circular import at module load time.
    from .scanner import parse_exiftool_datetime

    def time_matches(intended: str, read) -> bool:
        return parse_exiftool_datetime(read) == parse_exiftool_datetime(intended)

    if "time" in change:
        t = change["time"]
        is_video = is_quicktime_video(Path(change["path"]).suffix)
        if is_video:
            # capture (DTO/create) -> QuickTime:CreateDate; modify -> ModifyDate
            capture = t.get("datetime_original") or t.get("create_date")
            if capture:
                read = get_tag(record, "CreateDate", ["QuickTime"]) or get_tag(
                    record, "CreationDate", ["Keys"]
                )
                if not time_matches(capture, read):
                    return False
            if t.get("modify_date"):
                if not time_matches(t["modify_date"],
                                    get_tag(record, "ModifyDate", ["QuickTime"])):
                    return False
        else:
            # Verify EVERY intended field, not just datetime_original.
            checks = [
                (t.get("datetime_original"), "DateTimeOriginal",
                 ["ExifIFD", "IFD0", "XMP-exif"]),
                (t.get("create_date"), "CreateDate", ["ExifIFD", "IFD0", "XMP-xmp"]),
                (t.get("modify_date"), "ModifyDate", ["IFD0", "ExifIFD"]),
            ]
            for intended, tag, groups in checks:
                if intended and not time_matches(intended, get_tag(record, tag, groups)):
                    return False

    if "gps" in change:
        g = change["gps"]
        coords = read_gps(record)
        if coords is None:
            return False
        if abs(coords[0] - float(g["lat"])) > 1e-4 or abs(coords[1] - float(g["lon"])) > 1e-4:
            return False

    # Provenance, when present and the config is available to write it.
    if HAS_CONFIG and "provenance" in change:
        p = change["provenance"]
        prov_checks = [
            (p.get("time_source"), "TimeSource"),
            (p.get("time_confidence"), "TimeConfidence"),
            (p.get("gps_source"), "GPSSource"),
            (p.get("gps_confidence"), "GPSConfidence"),
        ]
        for intended, tag in prov_checks:
            if intended and get_tag(record, tag, ["XMP-exifheal"]) != intended:
                return False

    return True


def write_via_argfile(
    argfile_path: Path,
    expected_changes: list[dict],
) -> tuple[list[str], int, str]:
    """Execute an exiftool write using an argfile, then VERIFY by re-reading.

    Runs: exiftool -overwrite_original_in_place -P -@ <argfile>

    Success is determined by re-reading each file and confirming the intended
    time/GPS values actually landed — NOT by parsing exiftool's stdout counts.
    The stdout counts are unreliable: "Nothing to do." is emitted on stderr,
    and the per-batch lines can desync from file order, so a no-op batch would
    otherwise cause the wrong file to be marked applied.

    `expected_changes` is the list of change dicts (each with "path" and
    optional "time"/"gps"). Returns (successfully_written_paths, error_count,
    stderr_output).
    """
    cmd = [
        EXIFTOOL,
        *CONFIG_ARGS,
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
        return [], len(expected_changes), "timeout"

    stderr = result.stderr or ""

    # Verify by re-reading the files and comparing against intended values.
    paths = [c["path"] for c in expected_changes]
    records = batch_read_files([Path(p) for p in paths])
    by_source: dict[str, dict] = {}
    for rec in records:
        source = rec.get("SourceFile")
        if source is not None:
            by_source[str(Path(source).resolve())] = rec

    successfully_written = []
    for change in expected_changes:
        path = change["path"]
        rec = by_source.get(str(Path(path).resolve()))
        if rec is not None and _change_landed(change, rec):
            successfully_written.append(path)

    error_count = len(expected_changes) - len(successfully_written)
    return successfully_written, error_count, stderr
