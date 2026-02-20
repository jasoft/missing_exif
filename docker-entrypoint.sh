#!/bin/sh
set -eu

LOCAL_SCRIPT="${MISSING_EXIF_SCRIPT:-/workspace/fill_missing_exif.py}"
BUNDLED_SCRIPT="/app/fill_missing_exif.py"

if [ -f "$LOCAL_SCRIPT" ]; then
  SCRIPT_PATH="$LOCAL_SCRIPT"
elif [ -f "$BUNDLED_SCRIPT" ]; then
  SCRIPT_PATH="$BUNDLED_SCRIPT"
else
  echo "Error: fill_missing_exif.py not found." >&2
  echo "Checked: $LOCAL_SCRIPT and $BUNDLED_SCRIPT" >&2
  exit 1
fi

exec python "$SCRIPT_PATH" "$@"
