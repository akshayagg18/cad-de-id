#!/usr/bin/env bash
# harvest -> transform -> verify -> README, writing straight into the upload layout attempt-N/{corpus,code,README.txt}.
# Extra flags are passed to every step, e.g.  --no-rename-paths   or   --resume
set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"; source "$HERE/env.sh"
mkdir -p "$PRIVATE" "$ATTEMPT_DIR"; chmod 700 "$PRIVATE"
[ -d "$INPUT" ] || { echo "no $INPUT - run 10_download.sh first"; exit 1; }
cd "$SRC_ROOT"
python3 -m deid harvest   --input "$INPUT" --private "$PRIVATE" --jobs "$JOBS" "$@"
python3 -m deid transform --input "$INPUT" --output "$OUT_CORPUS" --private "$PRIVATE" --jobs "$JOBS" "$@"
python3 -m deid verify    --input "$INPUT" --output "$OUT_CORPUS" --private "$PRIVATE" --jobs "$JOBS" --expected-files "$EXPECTED_FILES"
python3 -m deid readme    --private "$PRIVATE" --attempt "$ATTEMPT" --out "$OUT_README"
bash "$HERE/30_package_code.sh"
# the same gate 40_upload.sh enforces, so a failure shows up now rather than at upload time
python3 -m deid gate --output "$OUT_CORPUS" --private "$PRIVATE" --attempt-dir "$ATTEMPT_DIR" --expected-files "$EXPECTED_FILES"
echo "ready: $ATTEMPT_DIR  ($(find "$OUT_CORPUS" -type f | wc -l) corpus files)"
