# Troubleshooting

## 1. exiftool not found

Symptoms:
- `初始化失败: 未检测到 exiftool`

Actions:
1. Install exiftool on host, or run script in Docker image with exiftool preinstalled.
2. Verify with `exiftool -ver`.

## 2. Unicode decode errors on Windows

Symptoms:
- `UnicodeDecodeError: 'gbk' codec can't decode ...`

Notes:
- Bundled script already reads subprocess output as bytes and decodes safely.
- If error still appears, verify the running script path points to this skill's `scripts/` version.

## 3. File format mismatch

Symptoms:
- `Error: Not a valid JPG (looks more like a RIFF)`

Actions:
1. Keep the file in failed list and skip forced writing.
2. Inspect actual format (`file` command on Linux/macOS, or media properties on Windows).
3. Rename or transcode only after confirming original content.

## 4. Permission denied during rollback/write

Symptoms:
- `PermissionError: [Errno 13] Permission denied ...`

Actions:
1. Enable retries: `--retry-until-success --retry-interval-seconds 10`.
2. Ensure target share is mounted with write permissions.
3. Check whether another process is locking the file.

## 5. Reuse staged artifacts

Behavior:
- Pipeline stores `discover_*.jsonl` and `plan_*.jsonl` under `<backup_dir>/.missing_exif_state/`.

Actions:
1. Re-run full pipeline to reuse files automatically.
2. Use `--refresh-discover` or `--refresh-filter` to force rebuild.
3. Run `write` directly with existing `plan.jsonl` for quick retries.
