# exif-heal Implementation Plan

## Overview

A Python CLI tool that scans a photo library (~55K files under `/photos/`) and repairs missing EXIF timestamps and GPS data before Immich import. About 3,500-5,000 files are missing DateTimeOriginal (mainly messenger `received_*` files and pre-2003 legacy photos).

Uses `exiftool` (v13.25, installed at `/usr/bin/exiftool`) as the read/write backend.

---

## Hard Rules

1. **JSON only**: All exiftool reads via `exiftool -j -n -G1 -api IgnoreMinorErrors=1`. Never parse text/pretty output anywhere in the codebase. No `-p`, no awk, no text parsing.
2. **Never overwrite existing tags** unless `--force`.
3. **Dry-run by default**; `--commit` required to write.
4. **Idempotent**: running scan+apply twice produces zero further changes.
5. **Always write XMP provenance tags** (ExifHealTimeSource, ExifHealGPSSource, ExifHealTimeConfidence, ExifHealGPSConfidence) so placeholders are filterable later. Disable with `--no-tag-provenance`.

---

## Project Structure

```
exif-heal/
    pyproject.toml                  # click dependency, [project.scripts] entry point
    src/
        exif_heal/
            __init__.py
            cli.py                  # click CLI: scan + apply subcommands
            models.py               # dataclasses: FileRecord, ProposedChange, Confidence enums
            exiftool.py             # subprocess wrapper: batch JSON read, argfile write
            cache.py                # SQLite cache: metadata + proposed changes persistence
            scanner.py              # orchestrator: walk dirs, read, infer, propose
            time_infer.py           # filename parsing, camera-session neighbor selection, interpolation
            gps_infer.py            # nearest neighbor GPS copy, haversine, centroid/outlier
            confidence.py           # confidence scoring + guardrail checks
            report.py               # JSONL writer + human-readable summary
            backup.py               # --backup-dir: copy originals preserving relative paths
            applier.py              # read proposed changes, generate argfile, run exiftool
    tests/
        conftest.py                 # pytest fixtures: synthetic EXIF image creation via exiftool
        test_time_infer.py
        test_gps_infer.py
        test_confidence.py
        test_cache.py
        test_exiftool.py            # integration (needs real exiftool)
        test_scanner.py             # integration
        test_cli.py                 # CLI smoke tests via click.testing.CliRunner
```

---

## CLI Design

```
exif-heal scan  --root <path> [options]    # read metadata, propose changes
exif-heal apply --root <path> [options]    # write proposed changes via exiftool
```

### Key flags

| Flag | Default | Purpose |
|------|---------|---------|
| `--root` | required | Directory to process |
| `--ext` | jpg,jpeg,dng,heic,png,mp4,mov,3gp | Comma-separated extensions |
| `--recursive/--no-recursive` | recursive | Recurse into subdirs |
| `--exclude-glob` | see below | Repeatable; glob patterns for dirs/files to skip |
| `--commit` | off | Actually write (default is dry-run) |
| `--report` | exif-heal-report.jsonl | JSONL output path |
| `--cache` | .exif-heal-cache.db | SQLite cache path |
| `--max-time-gap` | 21600 (6h) | Max seconds for neighbor interpolation |
| `--max-distance-km` | 50 | GPS jump guard radius |
| `--backup-dir` | none | Copy originals before modifying |
| `--only-missing-time` | off | Only process files missing timestamps |
| `--only-missing-gps` | off | Only process files missing GPS |
| `--limit` | none | Stop after N proposed changes |
| `--timezone` | none | IANA TZ for ambiguous times |
| `--allow-jumps` | off | Allow GPS beyond max-distance-km |
| `--allow-low-confidence` | off | Apply LOW confidence changes (default: MED+ only) |
| `--min-confidence-time` | med | Minimum confidence to apply time changes (high/med/low) |
| `--min-confidence-gps` | med | Minimum confidence to apply GPS changes (high/med/low) |
| `--force` | off | Overwrite existing EXIF tags |
| `--write-xmp-sidecar` | off | Force XMP sidecars for DNG |
| `--no-tag-provenance` | off | Disable writing XMP ExifHeal provenance tags |
| `--no-xmp-mirror` | off | Disable mirroring EXIF tags to XMP equivalents |
| `--default-gps` | none | Simple fallback GPS as "lat,lon" (single location for all files) |
| `--gps-hints` | none | Path to JSON file with time-period GPS defaults (see below) |
| `--print-plan` | off | Print table of proposed changes |

### Default excludes

These directories are skipped unless explicitly overridden with `--no-default-excludes`:
- `*/_Unsorted_LEGACY_DO_NOT_TOUCH/*`
- `*/ZZ_Private/*`

Additional excludes are added via repeatable `--exclude-glob`:
```
exif-heal scan --root /photos --exclude-glob "*/ZZ_old_photos/*" --exclude-glob "*/tmp/*"
```

---

## Core Algorithm

### Phase A: Read metadata (per directory)

One `exiftool -j -n -G1 -api IgnoreMinorErrors=1 <tags> <dir>/` call per directory. Non-recursive (we `os.walk` ourselves). Fields read:
- DateTimeOriginal, CreateDate, ModifyDate, XMP:DateCreated
- GPSLatitude, GPSLongitude
- FileModifyDate, FileName, FileSize
- Make, Model

### Phase B: Establish capture time hierarchy

For each file, walk the priority chain:
1. DateTimeOriginal (source=exif_dto)
2. CreateDate (source=exif_create)
3. ModifyDate (source=exif_modify)
4. XMP:DateCreated (source=xmp_created)
5. Parsed from filename (source=filename) - see patterns below
6. File mtime as last resort (source=mtime, confidence=LOW) - **skipped entirely if directory is flagged as bulk-copied**

Files with a source from steps 1-4 are "anchors" for neighbor interpolation.

### Phase C: Parse timestamps from filenames

Patterns to match:
- `received_YYYYMMDD_<random>.jpeg` - Messenger (date only, no time)
- `IMG_YYYYMMDD_HHMMSS.*` - Android/camera (full timestamp)
- `YYYYMMDD_HHMMSS.*` - Bare timestamp (full timestamp)
- `IMG-YYYYMMDD-WA*.jpg` - WhatsApp (date only)
- `Screenshot_YYYYMMDD-HHMMSS.*` - Screenshots (full timestamp)

Validation: reject dates before 1990 or after 2030, invalid month/day. Date-only patterns get midnight time with lower confidence.

### Phase D: Fill missing timestamps via neighbor interpolation

For files missing ALL of DateTimeOriginal/CreateDate/ModifyDate:

1. Sort all files in the directory by: capture_time (if known) > filename_time > file_mtime. Ties broken by filename.

2. **Camera session neighbor preference**: When selecting neighbors, prefer files with the same Make/Model as priority neighbors ("camera session" grouping). Rationale: photos from the same camera in the same folder are almost certainly from the same shooting session, making them far more reliable time references than a received messenger photo that happens to be closer in mtime.
   - First, look for same-Make/Model anchors within max-time-gap.
   - If none, fall back to any anchor within max-time-gap.

3. **Both neighbors within max-time-gap**: Linear interpolation by position fraction. Confidence=HIGH if same camera model on both sides, MED otherwise.
4. **One neighbor within max-time-gap**: Copy + small offset (1s per position). Confidence=MED.
5. **Filename time available, no EXIF neighbors**: Use filename time. Confidence=MED (full time) or LOW (date only).
6. **Only mtime available**: Use mtime. Confidence=LOW.

Guardrail: if inferred time differs from mtime by >2 years, force confidence=LOW and log loudly.

**Bulk-copy detection**: If >80% of files in a directory share the exact same mtime (within 60 seconds), flag the directory as "bulk-copied". In bulk-copied dirs:
- mtime is **not used as evidence** at all (not for sorting, not as fallback)
- Sort by filename only (after EXIF-anchored files)
- Rely solely on filename parsing and neighbor interpolation

### Phase E: Fill missing GPS

For files missing GPS that have a capture_time (original or just-inferred):

1. Find nearest-in-time file in same directory that HAS GPS, within max-time-gap.
2. **Neighbor found**: Copy its GPS. Confidence=HIGH if gap<1h, MED if gap<max-time-gap.
3. **No GPS neighbor, but GPS hint available**: Look up the file's capture time in the `--gps-hints` config (or fall back to `--default-gps`). Confidence=LOW, source=default_hint.
4. **No GPS at all**: Leave blank.

**GPS hints config** (`--gps-hints hints.json`):

Time-period-aware GPS defaults for files without GPS neighbors. Matches a file's inferred capture time to a date range:

```json
[
  {
    "from": "2000-01-01", "to": "2009-12-31",
    "lat": -34.881135, "lon": 138.459200,
    "label": "Adelaide - off West Lakes shore"
  },
  {
    "from": "2010-01-01", "to": "2014-10-31",
    "lat": -36.845, "lon": 174.770,
    "label": "Auckland - Waitemata Harbour"
  },
  {
    "from": "2014-11-01", "to": "2099-12-31",
    "lat": -34.881135, "lon": 138.459200,
    "label": "Adelaide - off West Lakes shore"
  }
]
```

All hint coords are in the water, making them instantly identifiable as placeholders on Immich's map. The label is written into the JSONL report for auditability. If `--default-gps` is also provided, it serves as the fallback when no hint range matches.

Centroid check: compute mean lat/lon of all GPS-bearing files in the folder. If proposed GPS >max-distance-km from centroid, downgrade confidence and skip unless --allow-jumps. (Does not apply to default-gps hints, which are always LOW and expected to be far from real clusters.)

### Phase F: Confidence gating

**Default behavior**: Only changes with MED or higher confidence are applied. LOW confidence changes are recorded in the JSONL report and cache but skipped during apply.

To include LOW confidence changes:
- `--allow-low-confidence` - applies all changes regardless of confidence
- `--min-confidence-time low` - applies LOW+ for time, keeps MED+ for GPS
- `--min-confidence-gps low` - applies LOW+ for GPS, keeps MED+ for time

This means `--default-gps` and `--gps-hints` placeholder coords (always LOW) require explicit opt-in: `--allow-low-confidence` or `--min-confidence-gps low`.

### Phase G: What gets written

**EXIF tags written**:
- `DateTimeOriginal` - always, when time is inferred
- `CreateDate` - always, when time is inferred
- `ModifyDate` - **only when confidence >= MED and source != mtime** (avoid polluting ModifyDate with unreliable data)
- `GPSLatitude`, `GPSLongitude` - when GPS is inferred (with `-n`, exiftool handles Ref tags automatically)

**XMP mirror tags** (written by default, disable with `--no-xmp-mirror`):
- `XMP-xmp:DateCreated` - mirrors DateTimeOriginal
- `XMP-photoshop:DateCreated` - mirrors DateTimeOriginal
- `XMP-exif:GPSLatitude`, `XMP-exif:GPSLongitude` - mirrors GPS

**XMP provenance tags** (always written unless `--no-tag-provenance`):
- `XMP-xmp:ExifHealTimeSource` = exif_dto|exif_create|filename|neighbor_interp|neighbor_copy|mtime|default_hint
- `XMP-xmp:ExifHealTimeConfidence` = high|med|low
- `XMP-xmp:ExifHealGPSSource` = exif|neighbor_copy|default_hint|none
- `XMP-xmp:ExifHealGPSConfidence` = high|med|low|none

These provenance tags make it trivial to filter in Immich/Lightroom: search for ExifHealGPSSource=default_hint to find all placeholder-GPS photos.

### Phase H: Sanity checks

- Never touch existing tags unless `--force`
- Time drift >2 years from mtime: force LOW
- GPS jump beyond centroid: downgrade or skip (except default-gps which is expected to be far)
- Idempotency: running scan+apply twice produces zero further changes (re-scan sees tags are now present, provenance tags already written)

---

## Data Flow

```
SCAN:  walk dirs (respecting --exclude-glob)
       -> exiftool batch JSON read per dir
       -> cache (SQLite) with freshness check
       -> parse filenames -> detect bulk-copy dirs
       -> sort (camera-session aware)
       -> infer times (neighbor interpolation)
       -> infer GPS (neighbor copy / default-gps)
       -> confidence scoring + guardrails
       -> write ProposedChanges to cache + JSONL report

APPLY: read pending changes from cache
       -> filter by confidence gate (MED+ default)
       -> backup originals to --backup-dir
       -> generate exiftool argfile (EXIF + XMP mirror + provenance tags)
       -> exiftool -overwrite_original_in_place -P -@ argfile
       -> mark applied in cache
```

The SQLite cache is the contract between scan and apply. You can scan, inspect the JSONL, then apply later.

---

## Key Implementation Details

### ExifTool interaction — JSON only
- **Read**: `exiftool -j -n -G1 -api IgnoreMinorErrors=1 <specific-tags> <dir>/` - one call per directory, non-recursive
- **Write**: Generate argfile with `-execute` separators per file, run `exiftool -overwrite_original_in_place -P -@ argfile.txt`
- **DNG policy**: Write into file directly. If exiftool fails on a DNG, fallback to XMP sidecar. `--write-xmp-sidecar` forces sidecars.
- **No text parsing anywhere**: All metadata extraction goes through JSON. This is a hard constraint.

### SQLite cache schema
- `files` table: path (PK), directory, metadata_json, file_mtime, file_size, proposed_json, applied flag, confidence_time, confidence_gps
- `scan_runs` table: audit trail of scan invocations
- Cache invalidation by (path, mtime, size) tuple

### Backup strategy
- `--backup-dir` copies files preserving relative path structure from `--root`
- Uses `-overwrite_original_in_place -P` (no `*_original` clutter, preserves filesystem timestamps)

### JSONL report (per file)
```json
{
  "file": "/photos/Albums/.../received_20190210.jpeg",
  "action": "set_time",
  "old": {"DateTimeOriginal": null, "GPSLatitude": null},
  "new": {"DateTimeOriginal": "2019:02:10 00:00:00", "GPSLatitude": -34.881135},
  "confidence_time": "low",
  "confidence_gps": "low",
  "reason_time": "filename_date_only",
  "reason_gps": "default_hint",
  "neighbors_used": [],
  "time_source": "filename",
  "gps_source": "default_hint",
  "gated": true,
  "gate_reason": "confidence below threshold (med)"
}
```

The `gated` field indicates whether the change was blocked by confidence gating. Gated changes appear in the report but are not applied unless thresholds are lowered.

---

## Addressing Known Library Issues

1. **Messenger `received_*` files** (~41+ found): Date parsed from filename, no time component. Confidence=LOW for time (date-only). GPS from neighbors or `--default-gps`. Gated by default — requires `--allow-low-confidence` or `--min-confidence-time low`.

2. **Pre-2003 legacy files** (ZZ_old_photos, etc.): No EXIF at all. Mtime likely wrong from data migrations. Bulk-copy detection flags these dirs, disabling mtime as evidence. Filename parsing where possible. GPS hints config maps time periods to locations: pre-2010 and post-2014 -> off West Lakes shore, Adelaide; 2010-2014 -> Waitemata Harbour, Auckland.

3. **DNG/RAW files** (~14K files): Most already have DateTimeOriginal. Write directly into DNG, fallback to XMP sidecar on failure.

4. **Files with full EXIF** (~95% of library): Untouched unless `--force`. Provenance tags still written if missing (to mark them as "original EXIF" for completeness), controlled by `--no-tag-provenance`.

5. **Excluded dirs**: `_Unsorted_LEGACY_DO_NOT_TOUCH` and `ZZ_Private` skipped by default.

---

## Implementation Order

### Phase 1: Foundation
1. `pyproject.toml` - packaging with click dependency
2. `models.py` - all dataclasses, enums (Confidence, TimeSource, GPSSource, FileRecord, ProposedChange)
3. `exiftool.py` + `test_exiftool.py` - batch JSON read, argfile write via subprocess

### Phase 2: Cache + filename parsing
4. `cache.py` + `test_cache.py` - SQLite operations
5. `time_infer.py` (filename parsing only) + `test_time_infer.py`

### Phase 3: Core algorithms
6. `time_infer.py` (camera-session neighbor selection + interpolation) + tests
7. `gps_infer.py` + `test_gps_infer.py` - haversine, neighbor copy, centroid, outlier detection
8. `confidence.py` + `test_confidence.py` - scoring, guardrails, gating logic

### Phase 4: Orchestration
9. `scanner.py` - ties read + cache + inference + exclude-glob + bulk-copy detection together
10. `report.py` - JSONL output + summary printer
11. `backup.py` - file backup with relative paths
12. `applier.py` - argfile generation (EXIF + XMP mirror + provenance) + exiftool write + confidence gating

### Phase 5: CLI + integration
13. `cli.py` - click commands wiring everything, all flags
14. `test_scanner.py`, `test_cli.py` - integration tests
15. `conftest.py` - shared fixtures (synthetic EXIF images via exiftool)

### Phase 6: Validation on real data
16. Run scan on a small album with `--limit 20`, inspect JSONL
17. Run scan on full library (scan-only)
18. Apply to small subset with `--backup-dir --commit`
19. Verify idempotency (scan again = zero proposals)

---

## Verification Plan

1. **Unit tests**: `pytest tests/` - covers filename parsing, camera-session neighbor selection, interpolation, haversine, confidence grading, gating logic, cache operations, bulk-copy detection
2. **Integration tests**: `pytest tests/ -m integration` - needs exiftool, tests real JSON read/write roundtrips, XMP mirror, provenance tags
3. **Manual validation**:
   - `exif-heal scan --root /photos/Albums/"Holiday - Manila Jan-Feb 2019" --print-plan` - directory with known `received_*` files
   - Inspect JSONL report: verify camera-session neighbors preferred, confidence levels correct, gating applied to LOW
   - `exif-heal apply --root <test-dir> --backup-dir /tmp/exif-backups --commit` on a small directory
   - Verify with `exiftool -j -G1 -DateTimeOriginal -XMP:ExifHealTimeSource -XMP:ExifHealGPSSource <file>` that EXIF + XMP mirror + provenance tags all written correctly
   - Run scan again to confirm zero proposals (idempotency)
