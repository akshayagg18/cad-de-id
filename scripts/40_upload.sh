#!/usr/bin/env bash
# Upload attempt-N with the OFFICIAL upload.py. Get the one-hour token from the signed-in submission panel
# ("Get upload token") and keep it in the environment only - never in a file under attempt-N/ or in git.
# usage:  MACHINA_UPLOAD_TOKEN=... DEST=gs://builders-machina-submissions-1120/teams/<team-id>/attempt-1/ bash 40_upload.sh
set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"; source "$HERE/env.sh"
UPLOAD_PY="${UPLOAD_PY:-$WORK/upload.py}"
[ -f "$UPLOAD_PY" ] || { echo "Put the official upload.py at $UPLOAD_PY (or set UPLOAD_PY=...)"; exit 1; }
: "${MACHINA_UPLOAD_TOKEN:?set MACHINA_UPLOAD_TOKEN (1-hour token from the submission panel)}"
: "${DEST:?set DEST=gs://.../attempt-$ATTEMPT/  (trailing slash)}"
case "$DEST" in */) ;; *) echo "DEST must end with /"; exit 1;; esac
[ -n "$(ls -A "$OUT_CORPUS" 2>/dev/null)" ] || { echo "corpus/ is empty"; exit 1; }
n=$(find "$OUT_CORPUS" -type f | wc -l); echo "objects in corpus: $n (limit 400000)"
[ "$n" -le 400000 ] || { echo "too many objects"; exit 1; }
find "$OUT_CORPUS" -type l | grep -q . && { echo "symlinks present (upload.py skips them)"; exit 1; }
find "$OUT_CORPUS" -type f -size +2G | grep -q . && echo "WARN: files > 2 GB are skipped by the scorer"
python3 "$UPLOAD_PY" --dest "$DEST" --corpus "$OUT_CORPUS" --code "$OUT_CODE" --readme "$OUT_README" --workers "${UP_WORKERS:-16}"
echo "uploaded. Now paste $DEST into the submission form."
