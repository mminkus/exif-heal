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
    # Resolve both sides so a relative or symlinked --root still produces a
    # correct relative path. Source paths come from the cache already resolved,
    # but root arrives straight from the CLI and may be relative.
    src = source.resolve()
    try:
        relative = src.relative_to(root.resolve())
    except ValueError:
        # Source is outside root: mirror its full directory structure under
        # backup_dir (drop the filesystem anchor) rather than collapsing to the
        # bare filename, which would let same-named files clobber each other.
        relative = Path(*src.parts[1:]) if src.is_absolute() else Path(src.name)

    dest = backup_dir / relative
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(src), str(dest))
    logger.debug("Backed up %s -> %s", src, dest)
    return dest
