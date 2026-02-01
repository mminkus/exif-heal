"""Apply proposed changes: read from cache, generate argfile, run exiftool."""

from __future__ import annotations

import json
import logging
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import exiftool
from .backup import backup_file
from .cache import MetadataCache
from .models import Confidence

logger = logging.getLogger(__name__)


@dataclass
class ApplySummary:
    """Summary of an apply run."""

    total_pending: int = 0
    gated_skipped: int = 0
    already_applied: int = 0
    backed_up: int = 0
    written: int = 0
    errors: int = 0
    dry_run: bool = True


def apply_changes(
    cache: MetadataCache,
    root: Path,
    commit: bool = False,
    backup_dir: Optional[Path] = None,
    min_confidence_time: Confidence = Confidence.MED,
    min_confidence_gps: Confidence = Confidence.MED,
    tag_provenance: bool = True,
    xmp_mirror: bool = True,
    write_xmp_sidecar: bool = False,
    limit: Optional[int] = None,
) -> ApplySummary:
    """Apply proposed changes from the cache.

    Reads pending changes, filters by confidence gate, backs up originals,
    generates an exiftool argfile, and runs exiftool.
    """
    summary = ApplySummary(dry_run=not commit)

    # Get all pending changes, scoped by root and with freshness check
    pending = cache.get_pending_changes(
        root=str(root.resolve()),
        check_freshness=True,
    )
    summary.total_pending = len(pending)

    if not pending:
        print("No pending changes to apply.")
        return summary

    # Filter by confidence and gating
    confidence_order = {
        "none": 0, "low": 1, "med": 2, "high": 3,
    }
    min_time_val = confidence_order.get(min_confidence_time.value, 2)
    min_gps_val = confidence_order.get(min_confidence_gps.value, 2)

    eligible = []
    for change in pending:
        if change.get("skipped"):
            summary.gated_skipped += 1
            continue

        has_time = "time" in change
        has_gps = "gps" in change

        # Re-evaluate gating at apply time using the apply's thresholds,
        # not the scan's pre-set gated_time/gated_gps flags
        provenance = change.get("provenance", {})
        time_conf_val = confidence_order.get(provenance.get("time_confidence", "none"), 0)
        gps_conf_val = confidence_order.get(provenance.get("gps_confidence", "none"), 0)

        time_gated = has_time and time_conf_val < min_time_val
        gps_gated = has_gps and gps_conf_val < min_gps_val

        # Build the effective change (strip gated parts)
        effective = {"path": change["path"]}
        has_anything = False

        if has_time and not time_gated:
            effective["time"] = change["time"]
            has_anything = True

        if has_gps and not gps_gated:
            effective["gps"] = change["gps"]
            has_anything = True

        if tag_provenance and "provenance" in change:
            effective["provenance"] = change["provenance"]

        if has_anything:
            eligible.append(effective)
        else:
            summary.gated_skipped += 1

    if limit:
        eligible = eligible[:limit]

    if not eligible:
        print(f"No eligible changes after confidence gating "
              f"(min_time={min_confidence_time.value}, min_gps={min_confidence_gps.value}).")
        print(f"  {summary.gated_skipped} changes were gated. "
              f"Use --allow-low-confidence or --min-confidence-* low to include them.")
        return summary

    # Print summary of what we'll do
    print(f"\nChanges to apply: {len(eligible)}")
    print(f"  Gated (skipped): {summary.gated_skipped}")
    if not commit:
        print("\n  DRY RUN — no files will be modified.")
        print("  Use --commit to actually write changes.")

        # Show a preview
        for change in eligible[:10]:
            path = change["path"]
            parts = []
            if "time" in change:
                parts.append(f"time={change['time'].get('datetime_original', '?')}")
            if "gps" in change:
                parts.append(f"gps={change['gps'].get('lat', '?')},{change['gps'].get('lon', '?')}")
            print(f"    {path}: {', '.join(parts)}")
        if len(eligible) > 10:
            print(f"    ... and {len(eligible) - 10} more")

        print()
        summary.written = len(eligible)  # Would-be count
        return summary

    # Backup originals
    if backup_dir:
        print(f"\nBacking up originals to {backup_dir}...")
        backup_path = Path(backup_dir)
        for change in eligible:
            source = Path(change["path"])
            if source.exists():
                try:
                    backup_file(source, root, backup_path)
                    summary.backed_up += 1
                except Exception as e:
                    logger.error("Failed to backup %s: %s", source, e)
                    summary.errors += 1

        print(f"  Backed up {summary.backed_up} files.")

    # Generate argfile
    argfile_content = exiftool.generate_argfile(
        eligible,
        tag_provenance=tag_provenance,
        xmp_mirror=xmp_mirror,
    )

    if not argfile_content.strip():
        print("No argfile content generated.")
        return summary

    # Write argfile and run exiftool
    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".args",
        prefix="exif-heal-",
        delete=False,
    ) as f:
        f.write(argfile_content)
        argfile_path = Path(f.name)

    try:
        print(f"\nRunning exiftool with {len(eligible)} file(s)...")
        expected_paths = [change["path"] for change in eligible]
        successfully_written, errors, stderr = exiftool.write_via_argfile(
            argfile_path, expected_paths
        )
        summary.written = len(successfully_written)
        summary.errors = errors

        if stderr:
            for line in stderr.strip().split("\n"):
                if line.strip():
                    logger.warning("exiftool: %s", line.strip())

        print(f"  Updated: {len(successfully_written)}")
        if errors:
            print(f"  Errors:  {errors}")

        # Mark only successfully written files as applied
        for path in successfully_written:
            cache.mark_applied(path)

        cache.commit()

    finally:
        # Clean up argfile
        try:
            argfile_path.unlink()
        except OSError:
            pass

    return summary
