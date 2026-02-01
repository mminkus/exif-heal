"""Data models for exif-heal."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional


class Confidence(Enum):
    """Confidence level for an inferred value."""

    HIGH = "high"
    MED = "med"
    LOW = "low"
    NONE = "none"

    def __ge__(self, other: Confidence) -> bool:
        order = {Confidence.NONE: 0, Confidence.LOW: 1, Confidence.MED: 2, Confidence.HIGH: 3}
        return order[self] >= order[other]

    def __gt__(self, other: Confidence) -> bool:
        order = {Confidence.NONE: 0, Confidence.LOW: 1, Confidence.MED: 2, Confidence.HIGH: 3}
        return order[self] > order[other]

    def __le__(self, other: Confidence) -> bool:
        order = {Confidence.NONE: 0, Confidence.LOW: 1, Confidence.MED: 2, Confidence.HIGH: 3}
        return order[self] <= order[other]

    def __lt__(self, other: Confidence) -> bool:
        order = {Confidence.NONE: 0, Confidence.LOW: 1, Confidence.MED: 2, Confidence.HIGH: 3}
        return order[self] < order[other]


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
    def has_exif_time(self) -> bool:
        """Whether any EXIF time tag is present."""
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
    gps_centroid_distance_km: float = 0.0
    gps_hint_label: str = ""

    # Gating
    skipped: bool = False
    skip_reason: str = ""
    gated_time: bool = False
    gated_gps: bool = False
    gate_reason: str = ""

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
    max_distance_km: float = 50.0
    only_missing_time: bool = False
    only_missing_gps: bool = False
    limit: Optional[int] = None
    timezone: Optional[str] = None
    allow_jumps: bool = False
    allow_low_confidence: bool = False
    min_confidence_time: Confidence = Confidence.MED
    min_confidence_gps: Confidence = Confidence.MED
    force: bool = False
    default_gps: Optional[GPSCoord] = None
    gps_hints: list[GPSHint] = field(default_factory=list)
    exclude_globs: list[str] = field(default_factory=list)
    no_default_excludes: bool = False
    write_xmp_sidecar: bool = False
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
