# exif-heal

A Python CLI tool that repairs missing EXIF timestamps and GPS data in photo libraries before importing into tools like Immich or Lightroom.

## Features

- **Smart timestamp inference**: Uses filename parsing, neighbor interpolation, and camera-session awareness to fill missing DateTimeOriginal tags
- **GPS propagation**: Copies GPS coordinates from nearby photos based on capture time
- **Bulk-copy detection**: Automatically detects and handles directories where files were bulk-copied (unreliable mtimes)
- **Confidence-based gating**: Only applies changes that meet configurable confidence thresholds (HIGH/MED/LOW)
- **SQLite cache**: Persistent metadata cache for fast re-scanning and idempotent apply operations
- **Provenance tracking**: Writes XMP tags documenting the source and confidence of inferred metadata
- **Safe by default**: Dry-run mode by default, optional backup-before-write, freshness checks prevent stale updates

## Installation

```bash
# Clone the repository
git clone https://github.com/yourusername/exif-heal.git
cd exif-heal

# Install with pip (in a virtual environment)
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
pip install -e .
```

**Requirement**: [ExifTool](https://exiftool.org/) must be installed and available in your PATH.

```bash
# On Debian/Ubuntu
apt install libimage-exiftool-perl

# On macOS
brew install exiftool
```

## Quick Start

```bash
# Scan a directory and see what would be changed
exif-heal scan --root /path/to/photos --print-plan

# Apply changes (dry-run by default)
exif-heal apply --root /path/to/photos

# Apply changes for real
exif-heal apply --root /path/to/photos --commit

# Apply with backups
exif-heal apply --root /path/to/photos --commit --backup-dir /backup/photos
```

## Usage

### Scan Command

```bash
exif-heal scan --root <directory> [options]
```

Walks the directory tree, reads EXIF metadata via ExifTool, infers missing timestamps and GPS, and writes proposed changes to a SQLite cache and JSONL report.

**Key options:**

- `--root PATH` - Directory to scan (required)
- `--recursive/--no-recursive` - Recurse into subdirectories (default: recursive)
- `--ext EXTENSIONS` - Comma-separated file extensions (default: jpg,jpeg,dng,heic,png,mp4,mov,3gp)
- `--exclude-glob PATTERN` - Exclude paths matching glob pattern (repeatable)
- `--limit N` - Stop after proposing N changes (useful for testing)
- `--print-plan` - Print table of proposed changes to stdout
- `--cache PATH` - SQLite cache path (default: .exif-heal-cache.db)
- `--report PATH` - JSONL report path (default: exif-heal-report.jsonl)

**Time inference options:**

- `--max-time-gap SECONDS` - Max time gap for neighbor interpolation (default: 21600 = 6 hours)
- `--only-missing-time` - Only process files missing timestamps
- `--force` - Propose changes even for files with existing EXIF

**GPS inference options:**

- `--max-distance-km KM` - Max GPS jump distance (default: 50)
- `--allow-jumps` - Allow GPS beyond max-distance-km
- `--default-gps LAT,LON` - Fallback GPS for files without neighbors
- `--gps-hints PATH` - JSON file with time-period GPS defaults (see below)
- `--only-missing-gps` - Only process files missing GPS

**Confidence gating:**

- `--min-confidence-time LEVEL` - Minimum confidence for time changes (high/med/low, default: med)
- `--min-confidence-gps LEVEL` - Minimum confidence for GPS changes (high/med/low, default: med)

### Apply Command

```bash
exif-heal apply --root <directory> [options]
```

Reads proposed changes from the cache and writes them to files via ExifTool.

**Key options:**

- `--root PATH` - Directory scope for apply (only apply changes under this path)
- `--cache PATH` - SQLite cache path (default: .exif-heal-cache.db)
- `--commit` - Actually write changes (default is dry-run)
- `--backup-dir PATH` - Copy originals before modifying (preserves relative paths)
- `--allow-low-confidence` - Apply LOW confidence changes (default: MED+ only)
- `--min-confidence-time LEVEL` - Re-evaluate time confidence threshold at apply time
- `--min-confidence-gps LEVEL` - Re-evaluate GPS confidence threshold at apply time
- `--no-tag-provenance` - Don't write XMP provenance tags
- `--no-xmp-mirror` - Don't mirror EXIF tags to XMP equivalents

## How It Works

### Timestamp Inference

For files missing DateTimeOriginal, exif-heal uses a hierarchy of sources:

1. **Filename parsing** - Extracts timestamps from patterns like:
   - `received_YYYYMMDD_xxx.jpeg` (Messenger)
   - `IMG_YYYYMMDD_HHMMSS.*` (Android/camera)
   - `Screenshot_YYYYMMDD-HHMMSS.*` (screenshots)
   - And [many more patterns](src/exif_heal/time_infer.py#L40-L90)

2. **Neighbor interpolation** - Finds nearby files with EXIF timestamps and:
   - Prefers same-camera neighbors (same Make/Model) for "camera session" grouping
   - Linear interpolation when surrounded by two anchors within `--max-time-gap`
   - Copy + small offset when only one neighbor is available

3. **File mtime fallback** - Uses filesystem modification time as last resort (skipped in bulk-copied directories)

**Bulk-copy detection**: If >80% of files share the same mtime (within 60 seconds), the directory is flagged as bulk-copied and mtime is not used as evidence.

**Confidence levels:**

- **HIGH**: Interpolated between two same-camera neighbors
- **MED**: Interpolated between mixed cameras, copied from one neighbor, or full timestamp from filename
- **LOW**: Date-only filename parse, mtime fallback, or drift >2 years from mtime

### GPS Inference

For files missing GPS coordinates that have a capture time (original or inferred):

1. **Neighbor copy** - Finds the nearest-in-time file in the same directory with GPS
   - **HIGH** confidence if gap <1 hour
   - **MED** confidence if gap <`--max-time-gap`

2. **GPS hints** - Time-period-aware defaults via `--gps-hints hints.json`:

   ```json
   [
     {
       "from": "2000-01-01", "to": "2009-12-31",
       "lat": -34.881135, "lon": 138.459200,
       "label": "Adelaide - West Lakes"
     },
     {
       "from": "2010-01-01", "to": "2014-10-31",
       "lat": -36.845, "lon": 174.770,
       "label": "Auckland - Waitemata Harbour"
     }
   ]
   ```

   Always **LOW** confidence, requires `--min-confidence-gps low` to apply.

3. **Default GPS** - Single fallback via `--default-gps lat,lon` (also LOW confidence)

**Guardrails:**

- GPS jumps beyond `--max-distance-km` from folder centroid are downgraded or skipped
- Default-gps and hints are exempt from jump checking (expected to be placeholders)

### Provenance Tags

By default, exif-heal writes XMP provenance tags to document metadata sources:

- `XMP-xmp:ExifHealTimeSource` = exif_dto|exif_create|filename|neighbor_interp|neighbor_copy|mtime|default_hint
- `XMP-xmp:ExifHealTimeConfidence` = high|med|low
- `XMP-xmp:ExifHealGPSSource` = exif|neighbor_copy|default_hint|none
- `XMP-xmp:ExifHealGPSConfidence` = high|med|low|none

This makes it trivial to filter in Immich/Lightroom: search for `ExifHealGPSSource=default_hint` to find all placeholder-GPS photos.

## Example Workflow

```bash
# 1. Scan a photo library
exif-heal scan --root /photos/Albums \
  --exclude-glob "*/_Unsorted_*" \
  --exclude-glob "*/ZZ_Private/*" \
  --gps-hints gps-hints.json \
  --print-plan

# 2. Review the JSONL report
cat exif-heal-report.jsonl | jq -c 'select(.gated==true)'  # See gated changes
cat exif-heal-report.jsonl | jq -c 'select(.confidence_gps=="low")'  # Low-confidence GPS

# 3. Apply high-confidence changes only
exif-heal apply --root /photos/Albums \
  --min-confidence-time high \
  --min-confidence-gps high \
  --backup-dir /backup/photos \
  --commit

# 4. Re-scan to verify idempotency (should show 0 proposals)
exif-heal scan --root /photos/Albums
```

## Testing

```bash
# Install dev dependencies
pip install -e ".[dev]"

# Run all tests (requires exiftool)
pytest tests/ -v

# Run only unit tests (no exiftool needed)
pytest tests/ -v -m "not integration"

# Run specific test file
pytest tests/test_time_infer.py -v
```

## Architecture

- [cli.py](src/exif_heal/cli.py) - Click CLI with scan/apply subcommands
- [scanner.py](src/exif_heal/scanner.py) - Scan orchestrator: walk dirs, read metadata, infer, propose
- [time_infer.py](src/exif_heal/time_infer.py) - Filename parsing, neighbor interpolation, bulk-copy detection
- [gps_infer.py](src/exif_heal/gps_infer.py) - GPS neighbor copy, hints, haversine distance, centroid checks
- [confidence.py](src/exif_heal/confidence.py) - Confidence scoring and gating logic
- [cache.py](src/exif_heal/cache.py) - SQLite metadata cache with freshness checks
- [exiftool.py](src/exif_heal/exiftool.py) - ExifTool subprocess wrapper (JSON-only, no text parsing)
- [applier.py](src/exif_heal/applier.py) - Apply proposed changes via ExifTool argfiles
- [models.py](src/exif_heal/models.py) - Dataclasses for FileRecord, ProposedChange, Confidence enums

## Design Principles

1. **JSON only** - All ExifTool reads via `exiftool -j -n -G1`. No text parsing anywhere.
2. **Never overwrite existing tags** unless `--force`
3. **Dry-run by default** - `--commit` required to write
4. **Idempotent** - Running scan+apply twice produces zero further changes
5. **Always write provenance tags** - Makes placeholders filterable in photo management tools

## Known Limitations

See [ISSUES.md](ISSUES.md) for tracked issues and future improvements.

- `--write-xmp-sidecar` flag accepted but not yet implemented
- `--timezone` flag accepted but not yet applied to parsing/inference
- Argfile generation doesn't handle filenames with newlines
- Provenance tags only written when changes are applied (not for files that already have complete EXIF)

## License

MIT License - see [LICENSE](LICENSE) for details.

## Contributing

Contributions welcome! Please:

1. Add tests for new features
2. Run `pytest tests/ -v` before submitting
3. Follow existing code style (respect the JSON-only ExifTool principle)

## Acknowledgments

Built on [ExifTool](https://exiftool.org/) by Phil Harvey - the gold standard for metadata manipulation.
