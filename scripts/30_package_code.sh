#!/usr/bin/env bash
# Copy the pipeline source into attempt-N/code/ (no private data, no tests' temp files, no caches).
set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"; source "$HERE/env.sh"
rm -rf "$OUT_CODE"; mkdir -p "$OUT_CODE"
cp -r "$SRC_ROOT/src" "$SRC_ROOT/scripts" "$SRC_ROOT/tests" "$OUT_CODE/"
cp "$SRC_ROOT/requirements.txt" "$SRC_ROOT/LICENSE" "$OUT_CODE/"
find "$OUT_CODE" \( -name __pycache__ -o -name .pytest_cache \) -prune -exec rm -rf {} +
# guard: nothing secret or bulky may ride along
if find "$OUT_CODE" -type f \( -name '*.tsv' -o -name 'salt*' -o -name '*.token' -o -name '.env' \) | grep -q .; then
  echo "refusing: private-looking file inside code/"; exit 1; fi
du -sh "$OUT_CODE"
