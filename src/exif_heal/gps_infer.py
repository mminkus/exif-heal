"""GPS inference: nearest neighbor copy, haversine distance, centroid/outlier detection."""

from __future__ import annotations

import logging
import math
from datetime import datetime
from typing import Optional

from .models import (
    Confidence,
    FileRecord,
    GPSCoord,
    GPSHint,
    GPSSource,
    ProposedChange,
)

logger = logging.getLogger(__name__)

EARTH_RADIUS_KM = 6371.0

# Default ceiling for plausible travel between two GPS fixes. Commercial flight
# cruises ~900 km/h; 1200 leaves margin. Anything faster around a photo implies
# a GPS data error, not real movement. Override via --max-speed-kmh / allow_jumps.
DEFAULT_MAX_SPEED_KMH = 1200.0


def haversine_km(a: GPSCoord, b: GPSCoord) -> float:
    """Haversine distance in km between two GPS coordinates."""
    lat1, lon1 = math.radians(a.lat), math.radians(a.lon)
    lat2, lon2 = math.radians(b.lat), math.radians(b.lon)

    dlat = lat2 - lat1
    dlon = lon2 - lon1

    h = (
        math.sin(dlat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    )
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(h))


def compute_folder_centroid(files: list[FileRecord]) -> Optional[GPSCoord]:
    """Compute mean lat/lon of all files with GPS in the folder."""
    gps_files = [f for f in files if f.gps is not None]
    if not gps_files:
        return None

    mean_lat = sum(f.gps.lat for f in gps_files) / len(gps_files)
    mean_lon = sum(f.gps.lon for f in gps_files) / len(gps_files)
    return GPSCoord(lat=mean_lat, lon=mean_lon)


def _effective_time(record: FileRecord, use_mtime: bool):
    """Time basis for GPS gap matching.

    When use_mtime is False (bulk-copied directory), file mtime is meaningless
    as evidence, so we require a real capture/filename time and return None
    otherwise — preventing bogus matches between files that merely share mtime.
    """
    t = record.capture_time or record.filename_time
    if t is None and use_mtime:
        t = record.file_mtime
    return t


def find_gps_neighbor(
    target: FileRecord,
    files: list[FileRecord],
    max_gap_seconds: int,
    use_mtime: bool = True,
) -> Optional[FileRecord]:
    """Find the file with GPS closest in capture_time to the target.

    Must be within max_gap_seconds. Returns None if no suitable neighbor.
    """
    target_time = _effective_time(target, use_mtime)
    if target_time is None:
        return None

    best: Optional[FileRecord] = None
    best_gap: float = float("inf")

    for f in files:
        if f.path == target.path:
            continue
        if f.gps is None:
            continue

        f_time = _effective_time(f, use_mtime)
        if f_time is None:
            continue

        gap = abs((f_time - target_time).total_seconds())
        if gap <= max_gap_seconds and gap < best_gap:
            best = f
            best_gap = gap

    return best


def find_gps_bracket(
    target: FileRecord,
    files: list[FileRecord],
    use_mtime: bool = True,
) -> tuple[Optional[FileRecord], Optional[FileRecord]]:
    """Nearest GPS-bearing files strictly before and after the target in time.

    Used by the speed-plausibility guardrail to check how fast the GPS field is
    moving around the target. Returns (before, after); either may be None.
    """
    tt = _effective_time(target, use_mtime)
    if tt is None:
        return None, None
    before = after = None
    before_dt = after_dt = None
    for f in files:
        if f.path == target.path or f.gps is None:
            continue
        ft = _effective_time(f, use_mtime)
        if ft is None:
            continue
        dt = (ft - tt).total_seconds()
        if dt < 0:
            if before is None or dt > before_dt:
                before, before_dt = f, dt
        elif dt > 0:
            if after is None or dt < after_dt:
                after, after_dt = f, dt
    return before, after


def lookup_gps_hint(
    capture_time: Optional[datetime],
    hints: list[GPSHint],
    default_gps: Optional[GPSCoord] = None,
) -> Optional[tuple[GPSCoord, str]]:
    """Look up GPS hint for a given capture time.

    Returns (coord, label) or None.
    """
    if capture_time and hints:
        for hint in hints:
            if hint.date_from <= capture_time <= hint.date_to:
                return hint.coord, hint.label

    if default_gps:
        return default_gps, "default_gps"

    return None


def infer_gps(
    files: list[FileRecord],
    max_time_gap: int,
    max_speed_kmh: float = DEFAULT_MAX_SPEED_KMH,
    allow_jumps: bool = False,
    default_gps: Optional[GPSCoord] = None,
    gps_hints: Optional[list[GPSHint]] = None,
    existing_changes: Optional[dict] = None,
    force: bool = False,
    use_mtime: bool = True,
) -> list[ProposedChange]:
    """Infer GPS for files missing it.

    Args:
        files: All FileRecords in one directory.
        max_time_gap: Maximum seconds between target and GPS donor.
        max_speed_kmh: Reject a copy only if the GPS field around the target
            moves faster than this (data error); travel across a multi-location
            folder is fine. allow_jumps downgrades instead of skipping.
        default_gps: Simple fallback GPS for all files.
        gps_hints: Time-period GPS hints.
        existing_changes: Dict mapping path -> ProposedChange from time inference,
            so we can merge GPS into existing changes rather than creating duplicates.
        force: If True, process files even if they already have GPS.

    Returns list of ProposedChanges for files that were missing GPS.
    """
    if existing_changes is None:
        existing_changes = {}

    changes = []

    for record in files:
        if record.has_gps and not force:
            continue

        neighbor = find_gps_neighbor(record, files, max_time_gap, use_mtime=use_mtime)

        coord: Optional[GPSCoord] = None
        confidence = Confidence.NONE
        source = GPSSource.NONE
        reason = ""
        hint_label = ""
        neighbors_gps: list[str] = []

        if neighbor is not None:
            coord = neighbor.gps
            neighbors_gps.append(str(neighbor.path))

            # Determine confidence based on time gap (same time basis as
            # neighbor selection — no mtime in bulk-copied dirs).
            target_time = _effective_time(record, use_mtime)
            neighbor_time = _effective_time(neighbor, use_mtime)
            gap = abs((target_time - neighbor_time).total_seconds())

            if gap < 3600:  # < 1 hour
                confidence = Confidence.HIGH
            else:
                confidence = Confidence.MED

            source = GPSSource.NEIGHBOR_COPY
            reason = f"copied from {neighbor.filename} (gap={gap:.0f}s)"

        elif gps_hints or default_gps:
            # Try GPS hints
            capture_time = record.capture_time or record.filename_time
            result = lookup_gps_hint(capture_time, gps_hints or [], default_gps)
            if result:
                coord, hint_label = result
                confidence = Confidence.LOW
                source = GPSSource.DEFAULT_HINT
                reason = f"GPS hint: {hint_label}"

        if coord is None:
            continue

        # Speed-plausibility guardrail (skip for default hints, expected to be
        # far). Replaces the folder-centroid check, which wrongly flagged
        # multi-location travel folders. Reject only when the GPS field around
        # the target moves at an impossible speed (data error): measure the
        # implied speed between the nearest GPS files before and after it.
        implied_speed = 0.0
        if source != GPSSource.DEFAULT_HINT:
            before, after = find_gps_bracket(record, files, use_mtime)
            if before is not None and after is not None:
                sep_km = haversine_km(before.gps, after.gps)
                bt = _effective_time(before, use_mtime)
                at = _effective_time(after, use_mtime)
                hours = abs((at - bt).total_seconds()) / 3600.0
                implied_speed = sep_km / hours if hours > 0 else float("inf")
                if implied_speed > max_speed_kmh:
                    if allow_jumps:
                        confidence = Confidence.LOW
                        reason += f" [GPS SPEED: {implied_speed:.0f}km/h around photo]"
                    else:
                        logger.warning(
                            "Implausible GPS speed for %s: %.0fkm/h (max=%s), skipping",
                            record.filename, implied_speed, max_speed_kmh,
                        )
                        change = ProposedChange(
                            path=record.path,
                            new_gps=coord,
                            gps_confidence=confidence,
                            gps_source=source,
                            reason_gps=reason,
                            gps_implied_speed_kmh=implied_speed,
                            skipped=True,
                            skip_reason=(
                                f"implausible GPS speed {implied_speed:.0f}km/h "
                                f"> {max_speed_kmh:.0f}km/h"
                            ),
                        )
                        changes.append(change)
                        continue

        # Merge into existing time change or create new
        path_str = str(record.path)
        if path_str in existing_changes:
            existing = existing_changes[path_str]
            existing.new_gps = coord
            existing.gps_confidence = confidence
            existing.gps_source = source
            existing.reason_gps = reason
            existing.neighbors_gps = neighbors_gps
            existing.gps_implied_speed_kmh = implied_speed
            existing.gps_hint_label = hint_label
        else:
            change = ProposedChange(
                path=record.path,
                new_gps=coord,
                gps_confidence=confidence,
                gps_source=source,
                reason_gps=reason,
                neighbors_gps=neighbors_gps,
                gps_implied_speed_kmh=implied_speed,
                gps_hint_label=hint_label,
            )
            changes.append(change)

    return changes
