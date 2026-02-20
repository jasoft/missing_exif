---
name: fill-missing-exif
description: Detect images and videos (including HEIF/HEIC) that lack capture-time metadata and write file mtime into EXIF/XMP/QuickTime tags with backup and dry-run safety. Use when copied media lost shooting time, large media libraries need batch repair, staged processing is required (discover/filter/write), or reusable plan files are needed for reruns.
---

# Fill Missing Exif

## Overview

Use this skill to repair missing capture-time metadata in photos and videos by writing filesystem mtime into metadata tags.

Run the bundled script in the installed skill directory:

- `python <skill-dir>/scripts/fill_missing_exif.py ...`

If the skill is installed to default location, `<skill-dir>` is usually:

- Windows: `%USERPROFILE%\\.codex\\skills\\fill-missing-exif`
- Linux/macOS: `$HOME/.codex/skills/fill-missing-exif`

## Workflow

1. Validate prerequisites: Python 3 and `exiftool` must be available.
2. Choose execution mode:
   - Full pipeline: `run` (default)
   - Stage only: `discover` / `filter` / `write`
3. Always run `--dry-run` first to preview changes.
4. Use `-y` only after confirming planned files and backup path.
5. For unstable NAS/SMB permissions, use `--retry-until-success`.

## Command Patterns

### Full pipeline (recommended)

```bash
python <skill-dir>/scripts/fill_missing_exif.py /data --backup-dir /backup --dry-run
```

### Stage split

```bash
python <skill-dir>/scripts/fill_missing_exif.py discover /data --backup-dir /backup --output /backup/.missing_exif_state/discover.jsonl
python <skill-dir>/scripts/fill_missing_exif.py filter /data --backup-dir /backup --input /backup/.missing_exif_state/discover.jsonl --output /backup/.missing_exif_state/plan.jsonl --scan-workers 32
python <skill-dir>/scripts/fill_missing_exif.py write /data --input /backup/.missing_exif_state/plan.jsonl --dry-run
```

### Retry failed writes until all success

```bash
python <skill-dir>/scripts/fill_missing_exif.py write /data --input /backup/.missing_exif_state/plan.jsonl -y --retry-until-success --retry-interval-seconds 10
```

## Output Behavior

- `discover`: print directory-by-directory pre-scan progress.
- `filter`: print one result line per processed record.
- `write`: print one result line per processed record.

## References

- Read `references/troubleshooting.md` when handling decode errors, damaged files, permission failures, or retry strategy decisions.
