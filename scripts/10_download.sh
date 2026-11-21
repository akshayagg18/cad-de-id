#!/usr/bin/env bash
# Download the full official dataset with the official helper (resumable, size-checked).
# Get get-input.sh from the Machina track page ("Input dataset" section). It is NOT bundled here.
set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"; source "$HERE/env.sh"
GETINPUT="${GETINPUT:-$WORK/get-input.sh}"
[ -f "$GETINPUT" ] || { echo "Put the official get-input.sh at $GETINPUT (or set GETINPUT=...)"; exit 1; }
mkdir -p "$WORK"; cd "$WORK"
bash "$GETINPUT" --jobs "${DL_JOBS:-16}"          # writes ./input/ ; re-run to resume
n=$(find "$INPUT" -type f -not -path '*/.get-input/*' | wc -l)
echo "files in input: $n (expected 188722)"
[ "$n" = 188722 ] || { echo "INCOMPLETE input; re-run this script"; exit 2; }
