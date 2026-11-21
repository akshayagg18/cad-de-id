#!/usr/bin/env bash
# Download the official dataset with the official helper (resumable, size-checked) into $INPUT.
# Get get-input.sh from the Machina track page; it is NOT bundled here.   Sample:  LIMIT=100 EXPECTED_FILES=100 bash 10_download.sh
# NEVER run this again once transform has started with CONSUME=1: consumed inputs would be downloaded a second time.
set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"; source "$HERE/env.sh"
GETINPUT="${GETINPUT:-$WORK/get-input.sh}"
[ -f "$GETINPUT" ] || { echo "Put the official get-input.sh at $GETINPUT (or set GETINPUT=...)"; exit 1; }
if [ -e "$PRIVATE/consumed.tsv" ] && [ -s "$PRIVATE/consumed.tsv" ]; then
  echo "refusing: $PRIVATE/consumed.tsv exists - inputs were already consumed; downloading again would re-fetch them."; exit 4
fi
mkdir -p "$WORK"; cd "$WORK"
args=(--dest "$INPUT" --jobs "${DL_JOBS:-16}")
[ -n "${PREFIX:-}" ] && args+=(--prefix "$PREFIX")
[ -n "${LIMIT:-}" ]  && args+=(--limit "$LIMIT")
bash "$GETINPUT" "${args[@]}"                       # re-run to resume
n=$(find "$INPUT" -type f -not -path '*/.get-input/*' | wc -l)
echo "files in input: $n (expected $EXPECTED_FILES)"
[ "$n" = "$EXPECTED_FILES" ] || { echo "INCOMPLETE input; re-run this script"; exit 2; }
