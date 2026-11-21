#!/usr/bin/env bash
# harvest -> transform -> verify -> README -> package code -> gate, writing straight into attempt-N/{corpus,code,README.txt}.
# Safe to re-run after an interruption. CONSUME=1 (default) deletes inputs once their outputs are durable; CONSUME=0 keeps them.
# Extra flags go to harvest and transform, e.g.  --no-rename-paths
set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"; source "$HERE/env.sh"; source "$HERE/lib.sh"
mkdir -p "$PRIVATE" "$ATTEMPT_DIR"; chmod 700 "$PRIVATE"
[ -d "$INPUT" ] || { echo "no $INPUT - run 10_download.sh first"; exit 1; }
stage_harvest "$@"
stage_transform "$@"
stage_verify
stage_readme
stage_package
# the same gate 40_upload.sh enforces, so a failure shows up now rather than at upload time
stage_gate
echo "ready: $ATTEMPT_DIR  ($(find "$OUT_CORPUS" -type f | wc -l) corpus files)"
