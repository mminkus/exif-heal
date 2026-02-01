"""JSONL report writer and human-readable summary printer."""

from __future__ import annotations

import json
from io import TextIOWrapper
from typing import Optional

from .models import FileRecord, ProposedChange, ScanSummary


def write_report_line(
    report_file: TextIOWrapper,
    record: FileRecord,
    proposed: ProposedChange,
):
    """Write one JSONL line for a proposed change."""
    entry = {
        "file": str(proposed.path),
        "action": _determine_action(proposed),
        "old": {
            "DateTimeOriginal": _fmt_dt(record.datetime_original),
            "CreateDate": _fmt_dt(record.create_date),
            "ModifyDate": _fmt_dt(record.modify_date),
            "GPSLatitude": record.gps.lat if record.gps else None,
            "GPSLongitude": record.gps.lon if record.gps else None,
        },
        "new": {
            "DateTimeOriginal": proposed.new_datetime_original,
            "CreateDate": proposed.new_create_date,
            "ModifyDate": proposed.new_modify_date,
            "GPSLatitude": proposed.new_gps.lat if proposed.new_gps else None,
            "GPSLongitude": proposed.new_gps.lon if proposed.new_gps else None,
        },
        "confidence_time": proposed.time_confidence.value,
        "confidence_gps": proposed.gps_confidence.value,
        "reason_time": proposed.reason_time,
        "reason_gps": proposed.reason_gps,
        "time_source": proposed.time_source.value,
        "gps_source": proposed.gps_source.value,
        "neighbors_used": proposed.neighbors_time + proposed.neighbors_gps,
        "mtime_drift_years": round(proposed.time_mtime_drift_years, 2),
        "gps_centroid_distance_km": round(proposed.gps_centroid_distance_km, 2),
    }

    if proposed.gps_hint_label:
        entry["gps_hint_label"] = proposed.gps_hint_label

    if proposed.skipped:
        entry["skipped"] = True
        entry["skip_reason"] = proposed.skip_reason

    if proposed.gated_time or proposed.gated_gps:
        entry["gated"] = True
        entry["gated_time"] = proposed.gated_time
        entry["gated_gps"] = proposed.gated_gps
        entry["gate_reason"] = proposed.gate_reason

    report_file.write(json.dumps(entry) + "\n")


def print_summary(summary: ScanSummary):
    """Print human-readable scan summary to stdout."""
    print()
    print("=" * 60)
    print("exif-heal scan summary")
    print("=" * 60)
    print(f"  Directories scanned:     {summary.dirs_scanned}")
    print(f"  Directories bulk-copied: {summary.dirs_bulk_copied}")
    print(f"  Files scanned:           {summary.files_scanned}")
    print(f"  Files missing time:      {summary.files_missing_time}")
    print(f"  Files missing GPS:       {summary.files_missing_gps}")
    print(f"  Time changes proposed:   {summary.files_proposed_time}")
    print(f"  GPS changes proposed:    {summary.files_proposed_gps}")
    print(f"  Changes gated (low conf):{summary.files_gated}")
    print(f"  Skipped (guardrails):    {summary.files_skipped_guardrails}")
    print("=" * 60)
    print()


def print_plan_table(changes: list[ProposedChange], limit: Optional[int] = None):
    """Print a table of proposed changes."""
    rows = changes[:limit] if limit else changes

    if not rows:
        print("No changes proposed.")
        return

    # Header
    print()
    print(f"{'File':<60} {'Time?':>5} {'GPS?':>4} {'TConf':>5} {'GConf':>5} {'Gated':>5}")
    print("-" * 95)

    for change in rows:
        filename = change.path.name if hasattr(change.path, 'name') else str(change.path).split("/")[-1]
        if len(filename) > 58:
            filename = "..." + filename[-55:]

        time_flag = "Y" if change.has_time_change else "-"
        gps_flag = "Y" if change.has_gps_change else "-"
        t_conf = change.time_confidence.value if change.has_time_change else "-"
        g_conf = change.gps_confidence.value if change.has_gps_change else "-"
        gated = "Y" if (change.gated_time or change.gated_gps) else "-"

        print(f"  {filename:<58} {time_flag:>5} {gps_flag:>4} {t_conf:>5} {g_conf:>5} {gated:>5}")

    if limit and len(changes) > limit:
        print(f"  ... and {len(changes) - limit} more")

    print()


def _determine_action(proposed: ProposedChange) -> str:
    if proposed.skipped:
        return "skip"
    if proposed.has_time_change and proposed.has_gps_change:
        return "set_both"
    if proposed.has_time_change:
        return "set_time"
    if proposed.has_gps_change:
        return "set_gps"
    return "skip"


def _fmt_dt(dt) -> Optional[str]:
    if dt is None:
        return None
    return dt.strftime("%Y:%m:%d %H:%M:%S")
