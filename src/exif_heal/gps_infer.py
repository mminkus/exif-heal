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


def find_gps_neighbor(
    target: FileRecord,
    files: list[FileRecord],
    max_gap_seconds: int,
) -> Optional[FileRecord]:
    """Find the file with GPS closest in capture_time to the target.

    Must be within max_gap_seconds. Returns None if no suitable neighbor.
    """
    target_time = target.capture_time or target.filename_time or target.file_mtime
    if target_time is None:
        return None

    best: Optional[FileRecord] = None
    best_gap: float = float("inf")

    for f in files:
        if f.path == target.path:
            continue
        if f.gps is None:
            continue

        f_time = f.capture_time or f.filename_time or f.file_mtime
        if f_time is None:
            continue

        gap = abs((f_time - target_time).total_seconds())
        if gap <= max_gap_seconds and gap < best_gap:
            best = f
            best_gap = gap

    return best


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
    max_distance_km: float,
    allow_jumps: bool = False,
    default_gps: Optional[GPSCoord] = None,
    gps_hints: Optional[list[GPSHint]] = None,
    existing_changes: Optional[dict] = None,
    force: bool = False,
) -> list[ProposedChange]:
    """Infer GPS for files missing it.

    Args:
        files: All FileRecords in one directory.
        max_time_gap: Maximum seconds between target and GPS donor.
        max_distance_km: Jump guard radius.
        allow_jumps: Allow GPS beyond max_distance_km.
        default_gps: Simple fallback GPS for all files.
        gps_hints: Time-period GPS hints.
        existing_changes: Dict mapping path -> ProposedChange from time inference,
            so we can merge GPS into existing changes rather than creating duplicates.
        force: If True, process files even if they already have GPS.

    Returns list of ProposedChanges for files that were missing GPS.
    """
    if existing_changes is None:
        existing_changes = {}

    centroid = compute_folder_centroid(files)
    changes = []

    for record in files:
        if record.has_gps and not force:
            continue

        neighbor = find_gps_neighbor(record, files, max_time_gap)

        coord: Optional[GPSCoord] = None
        confidence = Confidence.NONE
        source = GPSSource.NONE
        reason = ""
        hint_label = ""
        neighbors_gps: list[str] = []

        if neighbor is not None:
            coord = neighbor.gps
            neighbors_gps.append(str(neighbor.path))

            # Determine confidence based on time gap
            target_time = record.capture_time or record.filename_time or record.file_mtime
            neighbor_time = neighbor.capture_time or neighbor.filename_time or neighbor.file_mtime
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

        # Centroid jump check (skip for default hints — they're expected to be far)
        centroid_dist = 0.0
        if centroid and source != GPSSource.DEFAULT_HINT:
            centroid_dist = haversine_km(coord, centroid)
            if centroid_dist > max_distance_km:
                if allow_jumps:
                    confidence = Confidence.LOW
                    reason += f" [GPS JUMP: {centroid_dist:.1f}km from centroid]"
                else:
                    logger.warning(
                        "GPS jump for %s: %.1fkm from centroid (max=%s), skipping",
                        record.filename, centroid_dist, max_distance_km,
                    )
                    # Record as skipped
                    change = ProposedChange(
                        path=record.path,
                        new_gps=coord,
                        gps_confidence=confidence,
                        gps_source=source,
                        reason_gps=reason,
                        gps_centroid_distance_km=centroid_dist,
                        skipped=True,
                        skip_reason=f"GPS jump {centroid_dist:.1f}km > {max_distance_km}km",
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
            existing.gps_centroid_distance_km = centroid_dist
            existing.gps_hint_label = hint_label
        else:
            change = ProposedChange(
                path=record.path,
                new_gps=coord,
                gps_confidence=confidence,
                gps_source=source,
                reason_gps=reason,
                neighbors_gps=neighbors_gps,
                gps_centroid_distance_km=centroid_dist,
                gps_hint_label=hint_label,
            )
            changes.append(change)

    return changes
