"""Tests for time inference: filename parsing, neighbor selection, interpolation."""

from datetime import datetime
from pathlib import Path

import pytest

from exif_heal.models import Confidence, FileRecord, TimeSource
from exif_heal.time_infer import (
    detect_bulk_copy,
    establish_capture_time,
    find_time_neighbors,
    infer_times,
    interpolate_time,
    parse_filename_time,
    sort_key,
)


# --- Filename parsing tests ---

class TestParseFilenameTime:

    def test_received_messenger(self):
        dt, has_time = parse_filename_time("received_20190210_391175661657088.jpeg")
        assert dt == datetime(2019, 2, 10, 0, 0, 0)
        assert has_time is False

    def test_img_android(self):
        dt, has_time = parse_filename_time("IMG_20181202_213147.jpg")
        assert dt == datetime(2018, 12, 2, 21, 31, 47)
        assert has_time is True

    def test_bare_timestamp(self):
        dt, has_time = parse_filename_time("20200709_090419.jpg")
        assert dt == datetime(2020, 7, 9, 9, 4, 19)
        assert has_time is True

    def test_whatsapp(self):
        dt, has_time = parse_filename_time("IMG-20190315-WA0001.jpg")
        assert dt == datetime(2019, 3, 15, 0, 0, 0)
        assert has_time is False

    def test_screenshot(self):
        dt, has_time = parse_filename_time("Screenshot_20201225-143021.png")
        assert dt == datetime(2020, 12, 25, 14, 30, 21)
        assert has_time is True

    def test_vid_prefix(self):
        dt, has_time = parse_filename_time("VID_20190101_120000.mp4")
        assert dt == datetime(2019, 1, 1, 12, 0, 0)
        assert has_time is True

    def test_pxl_prefix(self):
        dt, has_time = parse_filename_time("PXL_20220315_181500.jpg")
        assert dt == datetime(2022, 3, 15, 18, 15, 0)
        assert has_time is True

    def test_no_match(self):
        dt, has_time = parse_filename_time("random_photo.jpg")
        assert dt is None
        assert has_time is False

    def test_invalid_month(self):
        dt, has_time = parse_filename_time("received_20191432_xxx.jpeg")
        assert dt is None

    def test_invalid_day(self):
        dt, has_time = parse_filename_time("IMG_20190235_120000.jpg")
        assert dt is None

    def test_year_too_old(self):
        dt, has_time = parse_filename_time("IMG_19800101_120000.jpg")
        assert dt is None

    def test_year_too_new(self):
        dt, has_time = parse_filename_time("IMG_20500101_120000.jpg")
        assert dt is None

    def test_dash_separated_datetime(self):
        dt, has_time = parse_filename_time("2018-06-17 12.17.33.jpg")
        assert dt == datetime(2018, 6, 17, 12, 17, 33)
        assert has_time is True


# --- Capture time hierarchy tests ---

class TestEstablishCaptureTime:

    def _make_record(self, **kwargs) -> FileRecord:
        defaults = dict(
            path=Path("/test/photo.jpg"),
            directory="/test",
            filename="photo.jpg",
            extension="jpg",
            file_mtime=datetime(2020, 1, 1),
            file_size=1000,
        )
        defaults.update(kwargs)
        return FileRecord(**defaults)

    def test_dto_takes_priority(self):
        r = self._make_record(
            datetime_original=datetime(2019, 6, 15, 10, 30),
            create_date=datetime(2019, 6, 15, 10, 31),
        )
        establish_capture_time(r)
        assert r.capture_time == datetime(2019, 6, 15, 10, 30)
        assert r.capture_time_source == TimeSource.EXIF_DTO

    def test_create_date_second(self):
        r = self._make_record(
            create_date=datetime(2019, 6, 15, 10, 31),
        )
        establish_capture_time(r)
        assert r.capture_time == datetime(2019, 6, 15, 10, 31)
        assert r.capture_time_source == TimeSource.EXIF_CREATE

    def test_modify_date_third(self):
        r = self._make_record(
            modify_date=datetime(2019, 6, 15, 10, 32),
        )
        establish_capture_time(r)
        assert r.capture_time == datetime(2019, 6, 15, 10, 32)
        assert r.capture_time_source == TimeSource.EXIF_MODIFY

    def test_xmp_created_fourth(self):
        r = self._make_record(
            xmp_date_created=datetime(2019, 6, 15, 10, 33),
        )
        establish_capture_time(r)
        assert r.capture_time == datetime(2019, 6, 15, 10, 33)
        assert r.capture_time_source == TimeSource.XMP_CREATED

    def test_filename_fifth(self):
        r = self._make_record(
            filename="IMG_20190615_103400.jpg",
        )
        establish_capture_time(r)
        assert r.capture_time == datetime(2019, 6, 15, 10, 34, 0)
        assert r.capture_time_source == TimeSource.FILENAME

    def test_no_time_at_all(self):
        r = self._make_record(filename="random.jpg")
        establish_capture_time(r)
        assert r.capture_time is None


# --- Bulk copy detection ---

class TestDetectBulkCopy:

    def _make_record(self, mtime: datetime) -> FileRecord:
        return FileRecord(
            path=Path("/test/photo.jpg"),
            directory="/test",
            filename="photo.jpg",
            extension="jpg",
            file_mtime=mtime,
            file_size=1000,
        )

    def test_not_bulk_copied(self):
        # Mtimes spread over hours — clearly not bulk-copied
        records = [
            self._make_record(datetime(2020, 1, 1, i, 0, 0))
            for i in range(10)
        ]
        assert detect_bulk_copy(records) is False

    def test_bulk_copied(self):
        same_time = datetime(2020, 6, 15, 12, 0, 0)
        records = [self._make_record(same_time) for _ in range(10)]
        assert detect_bulk_copy(records) is True

    def test_mostly_bulk_copied(self):
        same_time = datetime(2020, 6, 15, 12, 0, 0)
        records = [self._make_record(same_time) for _ in range(9)]
        records.append(self._make_record(datetime(2019, 1, 1)))
        assert detect_bulk_copy(records) is True

    def test_too_few_files(self):
        same_time = datetime(2020, 6, 15, 12, 0, 0)
        records = [self._make_record(same_time) for _ in range(2)]
        assert detect_bulk_copy(records) is False


# --- Neighbor selection ---

class TestFindTimeNeighbors:

    def _make_record(
        self,
        name: str,
        capture_time: datetime = None,
        source: TimeSource = None,
        has_time: bool = True,
        camera_key: str = None,
        file_mtime: datetime = None,
    ) -> FileRecord:
        r = FileRecord(
            path=Path(f"/test/{name}"),
            directory="/test",
            filename=name,
            extension="jpg",
            file_mtime=file_mtime or capture_time or datetime(2020, 1, 1),
            file_size=1000,
            capture_time=capture_time,
            capture_time_source=source,
            filename_time_has_time=has_time,
        )
        if camera_key:
            make, model = camera_key.split("|")
            r.make = make
            r.model = model
        return r

    def test_both_neighbors(self):
        files = [
            self._make_record("a.jpg", datetime(2020, 1, 1, 10, 0), TimeSource.EXIF_DTO),
            self._make_record("b.jpg", file_mtime=datetime(2020, 1, 1, 10, 30)),  # target
            self._make_record("c.jpg", datetime(2020, 1, 1, 11, 0), TimeSource.EXIF_DTO),
        ]
        before, after = find_time_neighbors(1, files, max_gap_seconds=21600)
        assert before is not None
        assert before.filename == "a.jpg"
        assert after is not None
        assert after.filename == "c.jpg"

    def test_only_before(self):
        files = [
            self._make_record("a.jpg", datetime(2020, 1, 1, 10, 0), TimeSource.EXIF_DTO),
            self._make_record("b.jpg", file_mtime=datetime(2020, 1, 1, 10, 30)),  # target
        ]
        before, after = find_time_neighbors(1, files, max_gap_seconds=21600)
        assert before is not None
        assert after is None

    def test_beyond_gap(self):
        files = [
            self._make_record("a.jpg", datetime(2020, 1, 1, 0, 0), TimeSource.EXIF_DTO),
            self._make_record("b.jpg", file_mtime=datetime(2020, 1, 2, 0, 0)),  # 24h away
        ]
        before, after = find_time_neighbors(1, files, max_gap_seconds=21600)
        assert before is None
        assert after is None

    def test_camera_session_preference(self):
        files = [
            self._make_record("a.jpg", datetime(2020, 1, 1, 10, 0), TimeSource.EXIF_DTO, camera_key="samsung|SM-N950U1"),
            self._make_record("b.jpg", datetime(2020, 1, 1, 10, 5), TimeSource.EXIF_DTO, camera_key="Canon|PowerShot"),
            self._make_record("c.jpg", camera_key="samsung|SM-N950U1",
                             file_mtime=datetime(2020, 1, 1, 10, 10)),  # target
            self._make_record("d.jpg", datetime(2020, 1, 1, 10, 15), TimeSource.EXIF_DTO, camera_key="samsung|SM-N950U1"),
        ]
        before, after = find_time_neighbors(
            2, files, max_gap_seconds=21600,
            prefer_camera_key="samsung|SM-N950U1",
        )
        # Should prefer samsung neighbors over Canon neighbor at 10:05
        assert before is not None
        assert before.filename == "a.jpg"
        assert after is not None
        assert after.filename == "d.jpg"


# --- Interpolation ---

class TestInterpolateTime:

    def _make_record(self, name: str, capture_time=None, source=None, mtime=None) -> FileRecord:
        return FileRecord(
            path=Path(f"/test/{name}"),
            directory="/test",
            filename=name,
            extension="jpg",
            file_mtime=mtime or datetime(2020, 1, 1),
            file_size=1000,
            capture_time=capture_time,
            capture_time_source=source,
        )

    def test_interpolate_between_two(self):
        files = [
            self._make_record("a.jpg", datetime(2020, 1, 1, 10, 0), TimeSource.EXIF_DTO),
            self._make_record("b.jpg"),  # target
            self._make_record("c.jpg", datetime(2020, 1, 1, 11, 0), TimeSource.EXIF_DTO),
        ]
        time, conf, source, reason = interpolate_time(1, files, files[0], files[2])
        assert time == datetime(2020, 1, 1, 10, 30)
        assert conf == Confidence.MED  # different cameras (None)
        assert source == TimeSource.NEIGHBOR_INTERP

    def test_interpolate_multiple_between(self):
        files = [
            self._make_record("a.jpg", datetime(2020, 1, 1, 10, 0), TimeSource.EXIF_DTO),
            self._make_record("b.jpg"),  # idx 1
            self._make_record("c.jpg"),  # idx 2
            self._make_record("d.jpg"),  # idx 3
            self._make_record("e.jpg", datetime(2020, 1, 1, 12, 0), TimeSource.EXIF_DTO),
        ]
        # b is at position 1 out of span 0-4
        time, _, _, _ = interpolate_time(1, files, files[0], files[4])
        # fraction = 1/4 = 0.25, delta = 2h, so 10:00 + 30min = 10:30
        assert time == datetime(2020, 1, 1, 10, 30)

    def test_copy_before_only(self):
        files = [
            self._make_record("a.jpg", datetime(2020, 1, 1, 10, 0), TimeSource.EXIF_DTO),
            self._make_record("b.jpg"),
        ]
        time, conf, source, _ = interpolate_time(1, files, files[0], None)
        assert time == datetime(2020, 1, 1, 10, 0, 1)  # +1s offset
        assert conf == Confidence.MED
        assert source == TimeSource.NEIGHBOR_COPY

    def test_copy_after_only(self):
        files = [
            self._make_record("a.jpg"),
            self._make_record("b.jpg", datetime(2020, 1, 1, 11, 0), TimeSource.EXIF_DTO),
        ]
        time, conf, source, _ = interpolate_time(0, files, None, files[1])
        assert time == datetime(2020, 1, 1, 10, 59, 59)  # -1s offset
        assert conf == Confidence.MED

    def test_fallback_to_filename(self):
        target = self._make_record("IMG_20200709_090419.jpg")
        target.filename_time = datetime(2020, 7, 9, 9, 4, 19)
        target.filename_time_has_time = True
        files = [target]
        time, conf, source, _ = interpolate_time(0, files, None, None)
        assert time == datetime(2020, 7, 9, 9, 4, 19)
        assert conf == Confidence.MED
        assert source == TimeSource.FILENAME

    def test_fallback_to_filename_date_only(self):
        target = self._make_record("received_20190210_xxx.jpeg")
        target.filename_time = datetime(2019, 2, 10)
        target.filename_time_has_time = False
        files = [target]
        time, conf, source, _ = interpolate_time(0, files, None, None)
        assert time == datetime(2019, 2, 10)
        assert conf == Confidence.LOW

    def test_fallback_to_mtime(self):
        target = self._make_record(
            "random.jpg",
            mtime=datetime(2020, 3, 15, 14, 0, 0),
        )
        files = [target]
        time, conf, source, _ = interpolate_time(0, files, None, None)
        assert time == datetime(2020, 3, 15, 14, 0, 0)
        assert conf == Confidence.LOW
        assert source == TimeSource.MTIME


# --- Full inference pipeline ---

class TestInferTimes:

    def _make_record(self, name, capture_time=None, source=None, mtime=None, has_exif=False):
        r = FileRecord(
            path=Path(f"/test/{name}"),
            directory="/test",
            filename=name,
            extension="jpg",
            file_mtime=mtime or datetime(2020, 1, 1),
            file_size=1000,
        )
        if has_exif:
            r.datetime_original = capture_time
            r.capture_time = capture_time
            r.capture_time_source = source or TimeSource.EXIF_DTO
        elif capture_time:
            r.capture_time = capture_time
            r.capture_time_source = source
        return r

    def test_skips_files_with_exif(self):
        files = [
            self._make_record("a.jpg", datetime(2020, 1, 1, 10, 0), has_exif=True),
        ]
        changes = infer_times(files, max_time_gap=21600)
        assert len(changes) == 0

    def test_infers_missing_time(self):
        files = [
            self._make_record("a.jpg", datetime(2020, 1, 1, 10, 0), has_exif=True),
            self._make_record("received_20200101_xxx.jpeg",
                             mtime=datetime(2020, 1, 1, 10, 30)),
            self._make_record("c.jpg", datetime(2020, 1, 1, 11, 0), has_exif=True),
        ]
        # Establish capture times
        for f in files:
            if not f.capture_time:
                establish_capture_time(f)

        changes = infer_times(files, max_time_gap=21600)
        assert len(changes) == 1
        assert changes[0].new_datetime_original is not None

    def test_no_mtime_in_bulk_copied(self):
        mtime = datetime(2020, 6, 15, 12, 0, 0)
        files = [
            self._make_record("random1.jpg", mtime=mtime),
            self._make_record("random2.jpg", mtime=mtime),
            self._make_record("random3.jpg", mtime=mtime),
        ]
        for f in files:
            establish_capture_time(f)

        changes = infer_times(files, max_time_gap=21600, use_mtime=False)
        # No EXIF, no filename time, no mtime allowed = no changes
        assert len(changes) == 0

    def test_force_reprocesses_exif_files(self):
        """--force should propose changes even for files with existing EXIF."""
        files = [
            self._make_record("a.jpg", datetime(2020, 1, 1, 10, 0), has_exif=True),
            self._make_record("b.jpg", datetime(2020, 1, 1, 10, 30), has_exif=True),
            self._make_record("c.jpg", datetime(2020, 1, 1, 11, 0), has_exif=True),
        ]
        for f in files:
            establish_capture_time(f)

        # Without force: no changes
        assert len(infer_times(files, max_time_gap=21600)) == 0

        # With force: all files get proposed changes
        changes = infer_times(files, max_time_gap=21600, force=True)
        assert len(changes) == 3

    def test_modify_date_cleared_on_drift(self):
        """ModifyDate should be unset when drift downgrades confidence to LOW."""
        files = [
            self._make_record("a.jpg", datetime(2020, 1, 1, 10, 0), has_exif=True),
            # Target: filename-parsed time is 2020, but mtime is far in the future
            self._make_record("received_20200101_xxx.jpeg",
                             mtime=datetime(2024, 6, 1, 12, 0, 0)),
            self._make_record("c.jpg", datetime(2020, 1, 1, 11, 0), has_exif=True),
        ]
        for f in files:
            if not f.capture_time:
                establish_capture_time(f)

        changes = infer_times(files, max_time_gap=21600)
        assert len(changes) == 1
        change = changes[0]
        # Drift > 2 years → confidence forced to LOW → ModifyDate cleared
        assert change.time_confidence == Confidence.LOW
        assert change.new_modify_date is None
        # But DateTimeOriginal and CreateDate still set
        assert change.new_datetime_original is not None
        assert change.new_create_date is not None
