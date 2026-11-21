#!/usr/bin/env bash
# Preflight: Linux, Python, deps, free disk, case-sensitive FS, hard-link support, reachability of the input host.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"; source "$HERE/env.sh"
mkdir -p "$WORK" || exit 1
fail=0
echo "WORK=$WORK  ATTEMPT=$ATTEMPT  JOBS=$JOBS"
[ "$(uname -s)" = Linux ] || { echo "FAIL: not Linux (the corpus has names that Windows/NTFS cannot hold)"; fail=1; }
python3 - <<'PY' || fail=1
import sys
assert sys.version_info >= (3, 10), "Python >= 3.10 required"
import olefile, pikepdf
print("python", sys.version.split()[0], "olefile", olefile.__version__, "pikepdf", pikepdf.__version__)
PY
avail_gb=$(df -P --block-size=1G "$WORK" | awk 'NR==2{print $4}')
echo "free space on WORK filesystem: ${avail_gb} GB (need >= ~150 GB for input + >= ~60 GB for rewritten output)"
[ "${avail_gb:-0}" -ge 210 ] || { echo "WARN: less than ~210 GB free"; }
t="$WORK/.case_test"; mkdir -p "$t"; : > "$t/a"; : > "$t/A"
[ "$(ls "$t" | wc -l)" = 2 ] && echo "case-sensitive FS: yes" || { echo "FAIL: case-insensitive filesystem"; fail=1; }
ln "$t/a" "$t/hl" 2>/dev/null && echo "hard links: yes" || echo "WARN: no hard links -> run transform with --no-link (needs a full second copy of the unchanged files)"
rm -rf "$t"
for h in machina-input.joinbuilders.org storage.googleapis.com; do
  code=$(curl -s -o /dev/null -m 15 -w '%{http_code}' "https://$h/" 2>/dev/null || echo 000)
  echo "https://$h/ -> HTTP $code"
done
[ "$fail" = 0 ] && echo "PREFLIGHT OK" || { echo "PREFLIGHT FAILED"; exit 1; }
