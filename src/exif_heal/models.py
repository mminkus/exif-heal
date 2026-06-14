"""Data models for exif-heal."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from functools import total_ordering
from pathlib import Path
from typing import Optional


# Video containers in the ISO Base Media / QuickTime family, whose metadata
# exiftool exposes under the QuickTime/Keys/UserData groups. Other video
# formats (AVI=RIFF, MKV=Matroska, WMV=ASF, MTS=AVCHD) use different tag
# families and are NOT handled by the QuickTime read/write paths.
QUICKTIME_VIDEO_EXTENSIONS = frozenset({"mp4", "mov", "m4v", "3gp", "3g2", "qt"})


def is_quicktime_video(extension: str) -> bool:
    """True if the (lowercase, dotless) extension is a QuickTime-family video."""
    return extension.lstrip(".").lower() in QUICKTIME_VIDEO_EXTENSIONS


_CONFIDENCE_ORDER = {"none": 0, "low": 1, "med": 2, "high": 3}


@total_ordering
class Confidence(Enum):
    """Confidence level for an inferred value."""

    HIGH = "high"
    MED = "med"
    LOW = "low"
    NONE = "none"

    @property
    def rank(self) -> int:
        """Ordinal rank (none=0 .. high=3) for comparison and sorting."""
        return _CONFIDENCE_ORDER[self.value]

    def __lt__(self, other: Confidence) -> bool:
        if self.__class__ is not other.__class__:
            return NotImplemented
        return self.rank < other.rank


class TimeSource(Enum):
    """How a capture time was determined."""

    EXIF_DTO = "exif_dto"
    EXIF_CREATE = "exif_create"
    EXIF_MODIFY = "exif_modify"
    XMP_CREATED = "xmp_created"
    FILENAME = "filename"
    NEIGHBOR_INTERP = "neighbor_interp"
    NEIGHBOR_COPY = "neighbor_copy"
    MTIME = "mtime"


class GPSSource(Enum):
    """How GPS coordinates were determined."""

    EXIF = "exif"
    NEIGHBOR_COPY = "neighbor_copy"
    DEFAULT_HINT = "default_hint"
    NONE = "none"


@dataclass
class GPSCoord:
    """A GPS coordinate pair."""

    lat: float
    lon: float


@dataclass
class GPSHint:
    """A time-period GPS default."""

    date_from: datetime
    date_to: datetime
    coord: GPSCoord
    label: str


@dataclass
class FileRecord:
    """One file's metadata as read from exiftool + filesystem."""

    path: Path
    directory: str
    filename: str
    extension: str  # lowercase, no dot
    file_mtime: datetime
    file_size: int

    # EXIF timestamps (None if missing)
    datetime_original: Optional[datetime] = None
    create_date: Optional[datetime] = None
    modify_date: Optional[datetime] = None
    xmp_date_created: Optional[datetime] = None

    # GPS (None if missing)
    gps: Optional[GPSCoord] = None

    # Camera info
    make: Optional[str] = None
    model: Optional[str] = None

    # Derived: best capture time from hierarchy
    capture_time: Optional[datetime] = None
    capture_time_source: Optional[TimeSource] = None

    # Filename-parsed time (always computed, used as evidence)
    filename_time: Optional[datetime] = None
    filename_time_has_time: bool = False  # True if filename had H:M:S

    @property
    def is_video(self) -> bool:
        """Whether this is a QuickTime-family video (mp4/mov/etc)."""
        return is_quicktime_video(self.extension)

    @property
    def has_exif_time(self) -> bool:
        """Whether any embedded time tag is present.

        For videos this reflects QuickTime/Keys dates, which
        ``record_from_exiftool`` maps onto these same fields.
        """
        return any([self.datetime_original, self.create_date, self.modify_date])

    @property
    def has_gps(self) -> bool:
        return self.gps is not None

    @property
    def camera_key(self) -> Optional[str]:
        """Make/Model key for camera session grouping. None if unknown."""
        if self.make and self.model:
            return f"{self.make}|{self.model}"
        return None


@dataclass
class ProposedChange:
    """What we want to write to a file."""

    path: Path

    # Time changes (None = no change proposed)
    new_datetime_original: Optional[str] = None  # "YYYY:MM:DD HH:MM:SS"
    new_create_date: Optional[str] = None
    new_modify_date: Optional[str] = None  # only set when confidence >= MED and source != mtime

    # GPS changes (None = no change proposed)
    new_gps: Optional[GPSCoord] = None

    # Confidence and provenance
    time_confidence: Confidence = Confidence.NONE
    time_source: TimeSource = TimeSource.MTIME
    gps_confidence: Confidence = Confidence.NONE
    gps_source: GPSSource = GPSSource.NONE

    # Audit trail
    reason_time: str = ""
    reason_gps: str = ""
    neighbors_time: list[str] = field(default_factory=list)
    neighbors_gps: list[str] = field(default_factory=list)

    # Guardrail flags
    time_mtime_drift_years: float = 0.0
    gps_implied_speed_kmh: float = 0.0
    gps_hint_label: str = ""

    # Gating
    skipped: bool = False
    skip_reason: str = ""
    gated_time: bool = False
    gated_gps: bool = False
    gate_reason: str = ""

    # GPS-specific skip (speed guardrail), recorded even when the file still has
    # a valid time change — so the audit survives dedup with that time change.
    gps_skipped: bool = False
    gps_skip_reason: str = ""

    @property
    def has_time_change(self) -> bool:
        return self.new_datetime_original is not None

    @property
    def has_gps_change(self) -> bool:
        return self.new_gps is not None

    @property
    def has_any_change(self) -> bool:
        return self.has_time_change or self.has_gps_change


@dataclass
class ScanConfig:
    """Configuration for a scan run."""

    root: Path
    extensions: list[str]
    recursive: bool = True
    max_time_gap: int = 21600  # 6 hours in seconds
    max_speed_kmh: float = 1200.0  # GPS-jump guard: max plausible travel speed
    only_missing_time: bool = False
    only_missing_gps: bool = False
    limit: Optional[int] = None
    allow_jumps: bool = False
    allow_low_confidence: bool = False
    min_confidence_time: Confidence = Confidence.MED
    min_confidence_gps: Confidence = Confidence.MED
    force: bool = False
    default_gps: Optional[GPSCoord] = None
    gps_hints: list[GPSHint] = field(default_factory=list)
    exclude_globs: list[str] = field(default_factory=list)
    no_default_excludes: bool = False
    no_tag_provenance: bool = False
    no_xmp_mirror: bool = False

    @property
    def effective_excludes(self) -> list[str]:
        """Exclude globs including defaults unless disabled."""
        defaults = [] if self.no_default_excludes else [
            "*/_Unsorted_LEGACY_DO_NOT_TOUCH/*",
            "*/ZZ_Private/*",
        ]
        return defaults + self.exclude_globs


@dataclass
class ScanSummary:
    """Summary statistics from a scan run."""

    files_scanned: int = 0
    files_missing_time: int = 0
    files_missing_gps: int = 0
    files_proposed_time: int = 0
    files_proposed_gps: int = 0
    files_gated: int = 0
    files_skipped_guardrails: int = 0
    dirs_scanned: int = 0
    dirs_bulk_copied: int = 0
