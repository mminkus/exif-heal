"""Shared test fixtures for exif-heal."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

import pytest


def _create_minimal_jpeg(filepath: Path):
    """Create a minimal valid JPEG that exiftool can read and write to.

    Uses exiftool to create a proper test image if available,
    otherwise writes a basic JFIF file.
    """
    try:
        # Use ImageMagick convert if available (creates proper JPEG)
        result = subprocess.run(
            ["convert", "-size", "1x1", "xc:white", str(filepath)],
            capture_output=True, timeout=5,
        )
        if result.returncode == 0 and filepath.exists():
            return
    except FileNotFoundError:
        pass

    # Fallback: write a minimal JFIF that exiftool can process
    # This is a valid 1x1 JPEG with proper structure
    import struct
    data = bytearray()
    # SOI
    data += b'\xff\xd8'
    # APP0 (JFIF)
    app0 = b'JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00'
    data += b'\xff\xe0' + struct.pack('>H', len(app0) + 2) + app0
    # DQT (quantization table)
    qt = bytes([8] * 64)
    data += b'\xff\xdb' + struct.pack('>H', len(qt) + 3) + b'\x00' + qt
    # SOF0 (start of frame)
    sof = struct.pack('>BHHB', 8, 1, 1, 1) + b'\x01\x11\x00'
    data += b'\xff\xc0' + struct.pack('>H', len(sof) + 2) + sof
    # DHT (Huffman table - DC)
    ht_dc = b'\x00' + bytes(16) + b'\x00'
    data += b'\xff\xc4' + struct.pack('>H', len(ht_dc) + 2) + ht_dc
    # DHT (Huffman table - AC)
    ht_ac = b'\x10' + bytes(16) + b'\x00'
    data += b'\xff\xc4' + struct.pack('>H', len(ht_ac) + 2) + ht_ac
    # SOS (start of scan)
    sos = struct.pack('>B', 1) + b'\x01\x00' + b'\x00\x3f\x00'
    data += b'\xff\xda' + struct.pack('>H', len(sos) + 2) + sos
    # Minimal scan data
    data += b'\x00\x00'
    # EOI
    data += b'\xff\xd9'
    filepath.write_bytes(bytes(data))


def has_exiftool() -> bool:
    """Check if exiftool is available."""
    try:
        result = subprocess.run(
            ["exiftool", "-ver"],
            capture_output=True, text=True, timeout=5,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


@pytest.fixture
def tmp_dir():
    """Create a temporary directory for tests."""
    d = tempfile.mkdtemp(prefix="exif-heal-test-")
    yield Path(d)
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def create_jpeg(tmp_dir):
    """Factory fixture to create JPEG files with optional EXIF data."""
    created_files = []

    def _create(
        name: str = "test.jpg",
        subdir: str = "",
        datetime_original: str | None = None,
        gps_lat: float | None = None,
        gps_lon: float | None = None,
        make: str | None = None,
        model: str | None = None,
        mtime: datetime | None = None,
    ) -> Path:
        if subdir:
            target_dir = tmp_dir / subdir
            target_dir.mkdir(parents=True, exist_ok=True)
        else:
            target_dir = tmp_dir

        filepath = target_dir / name
        # Create a minimal JPEG using exiftool's TestImage feature
        # If exiftool is available, use it to create a proper JPEG
        _create_minimal_jpeg(filepath)

        # Set EXIF data via exiftool if available
        if has_exiftool() and any([datetime_original, gps_lat, gps_lon, make, model]):
            cmd = ["exiftool", "-overwrite_original"]
            if datetime_original:
                cmd.extend([f"-DateTimeOriginal={datetime_original}"])
                cmd.extend([f"-CreateDate={datetime_original}"])
            if gps_lat is not None and gps_lon is not None:
                cmd.extend([f"-GPSLatitude={gps_lat}", f"-GPSLongitude={gps_lon}"])
            if make:
                cmd.extend([f"-Make={make}"])
            if model:
                cmd.extend([f"-Model={model}"])
            cmd.append(str(filepath))
            subprocess.run(cmd, capture_output=True, timeout=10)

        # Set mtime if specified
        if mtime:
            ts = mtime.timestamp()
            os.utime(str(filepath), (ts, ts))

        created_files.append(filepath)
        return filepath

    return _create


@pytest.fixture
def cache_db(tmp_dir):
    """Create a temporary cache database."""
    from exif_heal.cache import MetadataCache
    db_path = tmp_dir / "test-cache.db"
    cache = MetadataCache(db_path)
    yield cache
    cache.close()
