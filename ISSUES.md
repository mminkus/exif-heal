# Issues

Below is a consolidated list of issues found when comparing DESIGN.md/PLAN.md to the current implementation.
Updated with a re-review to mark fixes and new/remaining issues.

## Fixed (confirmed)
- `--force` is now honored in time and GPS inference and wired through the scanner. Files: `src/exif_heal/time_infer.py:339`, `src/exif_heal/gps_infer.py:136`, `src/exif_heal/scanner.py:252`.
- Apply-time confidence thresholds are now re-evaluated during apply (not just scan). File: `src/exif_heal/applier.py:79`.
- ModifyDate is cleared when confidence is downgraded to LOW due to drift. File: `src/exif_heal/time_infer.py:392`.
- GPS inference now uses just-inferred times by updating FileRecord.capture_time before GPS inference. File: `src/exif_heal/scanner.py:262`.
- FileSize parsing falls back to filesystem stat when exiftool returns a string. File: `src/exif_heal/scanner.py:70`.
- Cache now stores filesystem `st_mtime` directly (avoids timezone-stripped exiftool round-trip mismatches). File: `src/exif_heal/scanner.py:213`.
- Freshness check now compares against current filesystem stats and skips missing/inaccessible files. File: `src/exif_heal/cache.py:178`.
- Root scoping for apply now requires a proper directory prefix (avoids `/foo` matching `/foobar`). File: `src/exif_heal/cache.py:173`.

## High
- Apply success mapping can still misalign when exiftool outputs per-batch lines other than “image files updated”/“Nothing to do.” (e.g., “image files unchanged”), so wrong files may be marked applied. File: `src/exif_heal/exiftool.py:236`.

## Medium
- `--write-xmp-sidecar` is accepted but unused; no forced sidecar writes and no fallback to sidecar on DNG failures. Files: `src/exif_heal/applier.py:40-43`, `src/exif_heal/exiftool.py`.
- `--timezone` is accepted and stored but never applied to parsing or inference, so it has no effect. Files: `src/exif_heal/cli.py:97-150`, `src/exif_heal/models.py:189`.
- Plan rule “JSON only, no text parsing anywhere” is violated by parsing exiftool stdout text for success counts. File: `src/exif_heal/exiftool.py:236`.
- Root scoping still uses string prefixes (not `Path.resolve()`/`relative_to()`), so symlinks or path normalization differences can cause false negatives. File: `src/exif_heal/cache.py:173`.

## Low
- Bulk-copy mode still uses file mtime to compute time gaps for neighbor selection (target side), partially undermining the “don’t use mtime as evidence” rule. File: `src/exif_heal/time_infer.py:215-218`.
- Argfile generation cannot safely handle filenames containing newlines, which breaks the “safe with newlines” requirement. File: `src/exif_heal/exiftool.py:195`.
- Provenance tags are only written when changes are applied; there is no path to “always write provenance tags” for files that already have EXIF and need no changes, which the plan called for. File: `src/exif_heal/applier.py:100`.

## Spec Gaps / Not Implemented
- DESIGN.md `--strategy` flags (timestamp/gps strategies) are not implemented.
- DESIGN.md `--tag-confidence` flag is not implemented (provenance tags are always written unless `--no-tag-provenance`).

## Open Questions
- Should files reported as “image files unchanged” be considered applied (i.e., treated as success) or left pending for re-apply?
