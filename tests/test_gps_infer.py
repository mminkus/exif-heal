"""Tests for GPS inference: haversine, neighbor copy, centroid, outlier detection."""

from datetime import datetime
from pathlib import Path

import pytest

from exif_heal.gps_infer import (
    compute_folder_centroid,
    find_gps_neighbor,
    haversine_km,
    infer_gps,
    lookup_gps_hint,
)
from exif_heal.models import (
    Confidence,
    FileRecord,
    GPSCoord,
    GPSHint,
    GPSSource,
    TimeSource,
)


class TestHaversine:

    def test_same_point(self):
        a = GPSCoord(lat=-34.9285, lon=138.6007)
        assert haversine_km(a, a) == pytest.approx(0.0, abs=0.001)

    def test_known_distance_munich_berlin(self):
        munich = GPSCoord(lat=48.1351, lon=11.5820)
        berlin = GPSCoord(lat=52.5200, lon=13.4050)
        dist = haversine_km(munich, berlin)
        assert dist == pytest.approx(504, abs=5)  # ~504 km

    def test_adelaide_to_auckland(self):
        adelaide = GPSCoord(lat=-34.9285, lon=138.6007)
        auckland = GPSCoord(lat=-36.8485, lon=174.7633)
        dist = haversine_km(adelaide, auckland)
        assert 3100 < dist < 3300  # ~3,200 km

    def test_short_distance(self):
        a = GPSCoord(lat=-34.881135, lon=138.459200)  # West Lakes
        b = GPSCoord(lat=-34.9285, lon=138.6007)     # Adelaide CBD
        dist = haversine_km(a, b)
        assert 10 < dist < 20  # ~14 km


class TestCentroid:

    def test_simple_centroid(self):
        files = [
            _make_gps_record("a.jpg", gps=GPSCoord(-34.0, 138.0)),
            _make_gps_record("b.jpg", gps=GPSCoord(-36.0, 140.0)),
        ]
        c = compute_folder_centroid(files)
        assert c is not None
        assert c.lat == pytest.approx(-35.0)
        assert c.lon == pytest.approx(139.0)

    def test_no_gps_files(self):
        files = [_make_gps_record("a.jpg")]
        assert compute_folder_centroid(files) is None

    def test_single_file(self):
        files = [_make_gps_record("a.jpg", gps=GPSCoord(-34.5, 138.5))]
        c = compute_folder_centroid(files)
        assert c.lat == pytest.approx(-34.5)


class TestFindGPSNeighbor:

    def test_finds_nearest_by_time(self):
        target = _make_gps_record(
            "target.jpg",
            capture_time=datetime(2020, 1, 1, 10, 30),
        )
        files = [
            _make_gps_record(
                "far.jpg",
                capture_time=datetime(2020, 1, 1, 8, 0),
                gps=GPSCoord(-34.0, 138.0),
            ),
            _make_gps_record(
                "near.jpg",
                capture_time=datetime(2020, 1, 1, 10, 45),
                gps=GPSCoord(-34.5, 138.5),
            ),
            target,
        ]
        neighbor = find_gps_neighbor(target, files, max_gap_seconds=21600)
        assert neighbor is not None
        assert neighbor.filename == "near.jpg"

    def test_no_neighbor_beyond_gap(self):
        target = _make_gps_record(
            "target.jpg",
            capture_time=datetime(2020, 1, 1, 10, 0),
        )
        files = [
            _make_gps_record(
                "far.jpg",
                capture_time=datetime(2020, 1, 2, 10, 0),
                gps=GPSCoord(-34.0, 138.0),
            ),
            target,
        ]
        neighbor = find_gps_neighbor(target, files, max_gap_seconds=21600)
        assert neighbor is None

    def test_no_neighbor_without_gps(self):
        target = _make_gps_record(
            "target.jpg",
            capture_time=datetime(2020, 1, 1, 10, 0),
        )
        files = [
            _make_gps_record(
                "no_gps.jpg",
                capture_time=datetime(2020, 1, 1, 10, 5),
            ),
            target,
        ]
        neighbor = find_gps_neighbor(target, files, max_gap_seconds=21600)
        assert neighbor is None


class TestLookupGPSHint:

    def test_match_first_period(self):
        hints = [
            GPSHint(
                date_from=datetime(2000, 1, 1),
                date_to=datetime(2009, 12, 31),
                coord=GPSCoord(-34.881135, 138.459200),
                label="Adelaide",
            ),
            GPSHint(
                date_from=datetime(2010, 1, 1),
                date_to=datetime(2014, 10, 31),
                coord=GPSCoord(-36.845, 174.770),
                label="Auckland",
            ),
        ]
        result = lookup_gps_hint(datetime(2005, 6, 15), hints)
        assert result is not None
        coord, label = result
        assert label == "Adelaide"
        assert coord.lat == pytest.approx(-34.881135)

    def test_match_second_period(self):
        hints = [
            GPSHint(
                date_from=datetime(2000, 1, 1),
                date_to=datetime(2009, 12, 31),
                coord=GPSCoord(-34.881135, 138.459200),
                label="Adelaide",
            ),
            GPSHint(
                date_from=datetime(2010, 1, 1),
                date_to=datetime(2014, 10, 31),
                coord=GPSCoord(-36.845, 174.770),
                label="Auckland",
            ),
        ]
        result = lookup_gps_hint(datetime(2012, 3, 10), hints)
        assert result is not None
        coord, label = result
        assert label == "Auckland"

    def test_no_match_falls_to_default(self):
        hints = [
            GPSHint(
                date_from=datetime(2000, 1, 1),
                date_to=datetime(2009, 12, 31),
                coord=GPSCoord(-34.881135, 138.459200),
                label="Adelaide",
            ),
        ]
        default = GPSCoord(-35.0, 138.5)
        result = lookup_gps_hint(datetime(2020, 1, 1), hints, default)
        assert result is not None
        coord, label = result
        assert label == "default_gps"

    def test_no_hints_no_default(self):
        result = lookup_gps_hint(datetime(2020, 1, 1), [])
        assert result is None

    def test_no_capture_time_uses_default(self):
        default = GPSCoord(-35.0, 138.5)
        result = lookup_gps_hint(None, [], default)
        assert result is not None


class TestInferGPS:

    def test_copies_from_neighbor(self):
        files = [
            _make_gps_record(
                "a.jpg",
                capture_time=datetime(2020, 1, 1, 10, 0),
                gps=GPSCoord(-34.5, 138.5),
            ),
            _make_gps_record(
                "b.jpg",
                capture_time=datetime(2020, 1, 1, 10, 30),
            ),
        ]
        changes = infer_gps(files, max_time_gap=21600, max_speed_kmh=1200)
        assert len(changes) == 1
        assert changes[0].new_gps is not None
        assert changes[0].new_gps.lat == pytest.approx(-34.5)
        assert changes[0].gps_source == GPSSource.NEIGHBOR_COPY

    def test_uses_hint_when_no_neighbor(self):
        files = [
            _make_gps_record(
                "a.jpg",
                capture_time=datetime(2005, 6, 15),
            ),
        ]
        hints = [
            GPSHint(
                date_from=datetime(2000, 1, 1),
                date_to=datetime(2009, 12, 31),
                coord=GPSCoord(-34.881135, 138.459200),
                label="Adelaide",
            ),
        ]
        changes = infer_gps(
            files, max_time_gap=21600, max_speed_kmh=1200,
            gps_hints=hints,
        )
        assert len(changes) == 1
        assert changes[0].gps_source == GPSSource.DEFAULT_HINT
        assert changes[0].gps_confidence == Confidence.LOW

    def test_skips_files_with_gps(self):
        files = [
            _make_gps_record(
                "a.jpg",
                capture_time=datetime(2020, 1, 1),
                gps=GPSCoord(-34.5, 138.5),
            ),
        ]
        changes = infer_gps(files, max_time_gap=21600, max_speed_kmh=1200)
        assert len(changes) == 0

    def test_force_reprocesses_gps_files(self):
        """With force=True, files that already have GPS should be reprocessed."""
        files = [
            _make_gps_record(
                "anchor.jpg",
                capture_time=datetime(2020, 1, 1, 10, 0),
                gps=GPSCoord(-34.5, 138.5),
            ),
            _make_gps_record(
                "has_gps.jpg",
                capture_time=datetime(2020, 1, 1, 10, 15),
                gps=GPSCoord(-35.0, 139.0),
            ),
        ]
        # Without force: nothing proposed
        changes = infer_gps(files, max_time_gap=21600, max_speed_kmh=1200, force=False)
        assert len(changes) == 0

        # With force: has_gps.jpg gets a proposal from the anchor
        changes = infer_gps(files, max_time_gap=21600, max_speed_kmh=1200, force=True)
        assert len(changes) >= 1
        forced = [c for c in changes if c.path.name == "has_gps.jpg"]
        assert len(forced) == 1
        assert forced[0].new_gps is not None


class TestSpeedGuardrail:
    """Reject GPS copies only when the field moves at an impossible speed; real
    multi-location travel within a folder must pass."""

    def test_plausible_travel_accepted(self):
        files = [
            _make_gps_record("a.jpg", capture_time=datetime(2020, 1, 1, 10, 0),
                             gps=GPSCoord(-34.0, 138.0)),
            _make_gps_record("gap.jpg", capture_time=datetime(2020, 1, 1, 12, 0)),
            # ~460 km away, 5h later => ~92 km/h => fine
            _make_gps_record("b.jpg", capture_time=datetime(2020, 1, 1, 15, 0),
                             gps=GPSCoord(-34.0, 143.0)),
        ]
        changes = infer_gps(files, max_time_gap=21600, max_speed_kmh=1200)
        gap = [c for c in changes if c.path.name == "gap.jpg"]
        assert len(gap) == 1
        assert gap[0].new_gps is not None
        assert not gap[0].skipped

    def test_impossible_speed_skipped(self):
        files = [
            _make_gps_record("a.jpg", capture_time=datetime(2020, 1, 1, 10, 0),
                             gps=GPSCoord(-34.0, 138.0)),
            _make_gps_record("gap.jpg", capture_time=datetime(2020, 1, 1, 10, 5)),
            # ~460 km away, 10 min later => ~2760 km/h => impossible
            _make_gps_record("b.jpg", capture_time=datetime(2020, 1, 1, 10, 10),
                             gps=GPSCoord(-34.0, 143.0)),
        ]
        changes = infer_gps(files, max_time_gap=21600, max_speed_kmh=1200)
        gap = [c for c in changes if c.path.name == "gap.jpg"]
        assert len(gap) == 1 and gap[0].skipped
        assert gap[0].gps_implied_speed_kmh > 1200

    def test_allow_jumps_downgrades_instead_of_skipping(self):
        files = [
            _make_gps_record("a.jpg", capture_time=datetime(2020, 1, 1, 10, 0),
                             gps=GPSCoord(-34.0, 138.0)),
            _make_gps_record("gap.jpg", capture_time=datetime(2020, 1, 1, 10, 5)),
            _make_gps_record("b.jpg", capture_time=datetime(2020, 1, 1, 10, 10),
                             gps=GPSCoord(-34.0, 143.0)),
        ]
        changes = infer_gps(files, max_time_gap=21600, max_speed_kmh=1200,
                            allow_jumps=True)
        gap = [c for c in changes if c.path.name == "gap.jpg"]
        assert len(gap) == 1 and not gap[0].skipped
        assert gap[0].gps_confidence == Confidence.LOW


class TestBulkCopyGPS:
    """In a bulk-copied dir (identical mtimes, no real capture time), GPS must
    NOT be copied based on mtime proximity alone."""

    def _rec(self, name, gps=None, mtime=datetime(2023, 1, 1, 12, 0, 0)):
        return FileRecord(
            path=Path(f"/d/{name}"), directory="/d", filename=name,
            extension="jpg", file_mtime=mtime, file_size=1000, gps=gps,
        )

    def test_neighbor_skipped_without_mtime(self):
        donor = self._rec("donor.jpg", gps=GPSCoord(lat=-34.9, lon=138.6))
        target = self._rec("target.jpg")  # same mtime, no capture time
        assert find_gps_neighbor(target, [donor, target], 21600, use_mtime=False) is None
        # use_mtime=True would (wrongly) match on identical mtime
        assert find_gps_neighbor(target, [donor, target], 21600, use_mtime=True) is donor

    def test_infer_gps_no_bogus_copy_when_bulk(self):
        donor = self._rec("donor.jpg", gps=GPSCoord(lat=-34.9, lon=138.6))
        target = self._rec("target.jpg")
        changes = infer_gps([donor, target], max_time_gap=21600,
                            max_speed_kmh=1200, use_mtime=False)
        assert all(c.path.name != "target.jpg" for c in changes)
        # Confirm the bug WOULD trigger with mtime allowed (HIGH-conf copy).
        changes2 = infer_gps([donor, target], max_time_gap=21600,
                             max_speed_kmh=1200, use_mtime=True)
        bogus = [c for c in changes2 if c.path.name == "target.jpg"]
        assert bogus and bogus[0].gps_confidence == Confidence.HIGH


# --- Helpers ---

def _make_gps_record(
    name: str,
    capture_time: datetime = None,
    gps: GPSCoord = None,
) -> FileRecord:
    return FileRecord(
        path=Path(f"/test/{name}"),
        directory="/test",
        filename=name,
        extension="jpg",
        file_mtime=capture_time or datetime(2020, 1, 1),
        file_size=1000,
        capture_time=capture_time,
        capture_time_source=TimeSource.EXIF_DTO if capture_time else None,
        gps=gps,
    )
