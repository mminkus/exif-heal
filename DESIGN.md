From ChatGPT:

Yep. Here’s a Codex / Claude Code “build me this tool” spec that matches what you described, but keeps it realistic (safe defaults, dry-run, logs, reversible).

You can paste this whole thing into Codex/Claude Code/qwen2.5-coder.

⸻

Project: exif-heal — fill missing EXIF timestamps + GPS from neighbors

Goal

Write a CLI tool that scans a photo library and repairs missing metadata for files that were shared via Messenger/WhatsApp/IG/etc and had EXIF stripped.

It should:
	1.	Fill DateTimeOriginal / CreateDate / ModifyDate for files missing them.
	2.	Fill GPSLatitude / GPSLongitude (+ref) for files missing them.
	3.	Do it using “best available evidence”:
	•	Prefer adjacent photos in the same folder (by capture time) as a reference
	•	If no reliable neighbors, fall back to file mtime (with a confidence tag)
	4.	Write changes using ExifTool, not custom EXIF writers.

⸻

Constraints / Philosophy
	•	Never overwrite existing good metadata unless explicitly asked.
	•	Must support dry-run that prints exactly what would be written.
	•	Must produce a log (CSV or JSONL) of every change, including confidence/reason.
	•	Must be safe with spaces/newlines in paths (use -print0, avoid broken parsing).
	•	Must handle common formats: jpg/jpeg/heic/png/dng (gps/timestamps meaningful mainly for jpg/heic/dng; png often has none).
	•	Use exiftool as the “read and write backend”.

⸻

CLI Design

Command name: exif-heal

Example:

exif-heal scan --root "/luna/media/photos/Albums/Holiday - Europe 2018" --ext jpg,jpeg,dng --report report.jsonl
exif-heal apply --root "/luna/media/photos/Albums/Holiday - Europe 2018" --ext jpg,jpeg,dng --dry-run
exif-heal apply --root "/luna/media/photos/Albums/Holiday - Europe 2018" --ext jpg,jpeg,dng --commit --backup-dir /tmp/exif-backups

Flags:
	•	--root <path> (required)
	•	--ext <comma list> default: jpg,jpeg,dng,heic,png
	•	--recursive default true
	•	--commit (otherwise dry-run)
	•	--dry-run (default if no --commit)
	•	--report <path> write JSONL of proposed changes (default: exif-heal-report.jsonl)
	•	--cache <path> metadata cache (default: .exif-heal-cache.json)
	•	--max-time-gap <seconds> default: 6*3600 (neighbors beyond this are “not reliable”)
	•	--max-distance-km <km> default: 50 (if GPS would jump too far, reduce confidence)
	•	--strategy timestamp=neighbors,mtime (order of fallback)
	•	--strategy gps=neighbors
	•	--set-timezone <IANA TZ> optional; used only when converting ambiguous times
	•	--write-xmp-sidecar optional mode for DNG/RAW if you prefer not touching RAW (default OFF; by default write into file if possible)
	•	--tag-confidence if set, write XMP tags like XMP:ExifHealConfidenceTime=high|med|low and XMP:ExifHealSourceTime=neighbors|mtime, similarly for GPS.

⸻

Core Algorithm

Step A — Inventory & read metadata

For all candidate files:
	•	Use one exiftool pass to read machine-parseable output:
	•	-j -n -G1
	•	Read:
	•	FilePath, FileName, Directory
	•	FileModifyDate
	•	DateTimeOriginal, CreateDate, ModifyDate
	•	GPSLatitude, GPSLongitude
	•	Optional: GPSDateTime, OffsetTimeOriginal, TimeZone
	•	Model/Make (useful for heuristics)
	•	Build an in-memory list grouped by directory.

Step B — Decide “canonical capture time”

For each file define:
	•	capture_time priority:
	1.	DateTimeOriginal
	2.	CreateDate
	3.	ModifyDate
	4.	else None

Step C — Fill missing timestamps

For a file missing all of (DTO/Create/Modify):
	1.	Find nearest neighbors in same directory with known capture_time.
	•	Determine neighbors by sorting all files in dir by:
	•	If file has capture_time: that value
	•	Else: file mtime as a placeholder
	•	For the target file, choose nearest known-time file before/after.
	2.	If both before+after exist:
	•	If gap between them is “reasonable” (<= --max-time-gap):
	•	Interpolate capture_time for the target based on position in the sorted list OR linear interpolation by mtime within that bracket.
	•	Confidence: high if both neighbors within max gap and have same camera model; med otherwise.
	3.	If only one neighbor exists within max gap:
	•	Copy that neighbor’s time plus/minus a small offset based on sequence index (e.g. +1s).
	•	Confidence: med.
	4.	If no neighbors:
	•	Use file mtime as capture_time.
	•	Confidence: low.
	5.	Write:
	•	DateTimeOriginal, CreateDate, ModifyDate all set to chosen capture_time
	•	also consider setting XMP:DateCreated if present in your ecosystem (optional).
	•	note: Some (most?) messenger recieved files were renamed to follow this pattern in the past: received_20181110_1534768293847.jpeg and they embed a timestamp.

Important:
	•	Never touch time if DTO already exists unless --force-time.

Step D — Fill missing GPS

For a file missing GPS:
	1.	Look for nearest neighbors in same directory with GPS and capture_time.
	•	Prefer neighbors within:
	•	--max-time-gap and also within sensible distance (computed from neighbor GPS sets).
	2.	If both before+after with GPS:
	•	If time bracket is reasonable:
	•	Option 1 (simple): copy the closer neighbor’s GPS
	•	Option 2 (fancier): linear interpolate lat/lon by time fraction
	•	Confidence: high if both GPS neighbors exist and are close; else med.
	3.	If only one GPS neighbor:
	•	Copy that GPS.
	•	Confidence: med if within max-time-gap, else low.
	4.	If no GPS neighbors:
	•	Leave GPS blank.
	5.	Write GPS fields:
	•	GPSLatitude, GPSLongitude (in decimal if using -n)
	•	Also set GPSLatitudeRef/GPSLongitudeRef if required (ExifTool usually handles it)
	•	Optionally set GPSAltitude only if you have it (probably skip).

Important:
	•	Never overwrite existing GPS unless --force-gps.

Step E — Sanity checks & “wild jump” guardrails

Before writing GPS:
	•	If the folder already has many GPS points, compute a centroid and typical radius.
	•	If new GPS is > --max-distance-km away from centroid:
	•	downgrade confidence
	•	optionally skip unless --allow-jumps

Before writing time:
	•	If chosen time is wildly different from file mtime (e.g. years off), mark low confidence and still allow but log loudly.

⸻

Writing changes with ExifTool

Preferred method: ExifTool argfile

Generate an argfile for batch apply:

For each file with proposed changes, build an entry like:
	•	Time:
	•	-DateTimeOriginal=YYYY:MM:DD HH:MM:SS
	•	-CreateDate=...
	•	-ModifyDate=...
	•	GPS:
	•	-GPSLatitude=48.141286
	•	-GPSLongitude=11.577603

Run:

exiftool -overwrite_original_in_place -P -@ args.txt

If --backup-dir:
	•	Use -o style is awkward; instead:
	•	copy file to backup dir before modifying (preserve structure)
	•	or use -overwrite_original OFF (ExifTool keeps _original), but that creates *_original which you may hate.
So implement your own backup copy unless user says otherwise.

For DNG / RAW caution

Support mode:
	•	default: write into file (works often, but not always)
	•	optional --write-xmp-sidecar:
	•	write file.dng.xmp with GPS/time tags

⸻

Output / Reporting

For each file, output one JSONL line (for machine use), plus a human summary.

JSONL record fields:
	•	file
	•	action: set_time, set_gps, set_both, skip
	•	old: existing tags
	•	new: proposed tags
	•	confidence_time: high|med|low|none
	•	confidence_gps: high|med|low|none
	•	reason_time: neighbors|interpolated|mtime|none
	•	reason_gps: neighbors|interpolated|none
	•	neighbors_used: list of neighbor file paths (before/after)

Also print summary counts at end:
	•	files scanned
	•	files missing time
	•	files fixed time
	•	files missing gps
	•	files fixed gps
	•	skipped due to guardrails

⸻

Implementation Requirements
	•	Language: Python 3 (preferred) or Go; Python is fine.
	•	Use subprocess to call exiftool.
	•	Must be robust with special chars in file names:
	•	read exiftool JSON (-j)
	•	avoid parsing human output
	•	Build directory groups and sort deterministically.
	•	Include unit-testable functions:
	•	neighbor selection
	•	interpolation logic
	•	distance calc (Haversine)

⸻

Nice-to-haves
	•	--only-missing-time, --only-missing-gps
	•	--limit N for testing
	•	--print-plan prints a table-like plan
	•	--resume uses cache so re-runs are fast (maybe?)
