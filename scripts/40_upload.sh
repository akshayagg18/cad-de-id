#!/usr/bin/env bash
# Upload attempt-N with the OFFICIAL upload.py. Get the one-hour token from the signed-in submission panel
# ("Get upload token"). It is read from the environment or from TOKEN_FILE (mode 600, outside attempt-N/); it is never
# printed, never put on a command line and never written under attempt-N/ or git.
# usage:  DEST=gs://builders-machina-submissions-1120/teams/<team-id>/attempt-1/ TOKEN_FILE=$WORK/private/upload-token bash 40_upload.sh
#     or  MACHINA_UPLOAD_TOKEN=... DEST=... bash 40_upload.sh
# When the token runs out, upload.py waits for a NEW token to be written to TOKEN_FILE and then carries on.
set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"; source "$HERE/env.sh"
UPLOAD_PY="${UPLOAD_PY:-$WORK/upload.py}"
[ -f "$UPLOAD_PY" ] || { echo "Put the official upload.py at $UPLOAD_PY (or set UPLOAD_PY=...)"; exit 1; }
if [ -n "${TOKEN_FILE:-}" ]; then
  [ -s "$TOKEN_FILE" ] || { echo "token file $TOKEN_FILE is missing or empty"; exit 1; }
  MACHINA_UPLOAD_TOKEN="$(tr -d '\r\n ' < "$TOKEN_FILE")"; export MACHINA_UPLOAD_TOKEN
fi
: "${MACHINA_UPLOAD_TOKEN:?set MACHINA_UPLOAD_TOKEN or TOKEN_FILE (1-hour token from the submission panel)}"
: "${DEST:?set DEST=gs://.../attempt-$ATTEMPT/  (trailing slash)}"
case "$DEST" in */) ;; *) echo "DEST must end with /"; exit 1;; esac
# Refuse to upload anything that has not passed verify: report ok, exact file count, fresh report, no symlinks/temp
# files, only corpus/ code/ README.txt in the attempt directory, nothing private inside it.
python3 -m deid gate --output "$OUT_CORPUS" --private "$PRIVATE" --attempt-dir "$ATTEMPT_DIR" --expected-files "$EXPECTED_FILES" \
  || { echo "upload refused: fix the gate errors above (re-run scripts/20_run_pipeline.sh) - nothing was uploaded"; exit 1; }
find "$OUT_CORPUS" -type f -size +2G | grep -q . && echo "WARN: files > 2 GB are skipped by the scorer"
args=(--dest "$DEST" --corpus "$OUT_CORPUS" --code "$OUT_CODE" --readme "$OUT_README" --workers "${UP_WORKERS:-16}")
[ -n "${TOKEN_FILE:-}" ] && args+=(--token-file "$TOKEN_FILE")
python3 "$UPLOAD_PY" "${args[@]}"
echo "uploaded. Uploading alone does not enter the attempt: paste $DEST into the submission form on the track page."
