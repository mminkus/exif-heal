"""Backup logic: copy originals preserving relative paths."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)


def backup_file(source: Path, root: Path, backup_dir: Path) -> Path:
    """Copy source to backup_dir preserving relative path from root.

    Example:
        source = /photos/Albums/Trip/IMG_001.jpg
        root = /photos
        backup_dir = /tmp/exif-backups
        -> copies to /tmp/exif-backups/Albums/Trip/IMG_001.jpg

    Returns the destination path.
    """
    try:
        relative = source.relative_to(root)
    except ValueError:
        # If source is not under root, use the full path
        relative = Path(source.name)

    dest = backup_dir / relative
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(source), str(dest))
    logger.debug("Backed up %s -> %s", source, dest)
    return dest
