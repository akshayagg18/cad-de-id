# Shared settings for the run scripts. Source it; do not execute it.
# WORK must be a case-sensitive Linux filesystem with room for the input (~140 GB) plus the rewritten output.
# input/ and attempt-N/ must live on the SAME filesystem (hard links / moves between them).
# Everything the pipeline writes (input, output, private, temp, pip cache, status) stays under $WORK, so one
# directory (or one disposable WSL2 distro) holds all bulk data and removing it removes everything.
: "${WORK:=$PWD/work}"
: "${ATTEMPT:=1}"
export WORK
export INPUT="$WORK/input"                       # official get-input.sh target
# The official input has exactly this many files; verify and the upload gate refuse any other count.
# Small test runs override it:  EXPECTED_FILES=100
: "${EXPECTED_FILES:=188722}"; export EXPECTED_FILES
export PRIVATE="$WORK/private"                   # salt, identity list, path map, defect log  (SECRET, never uploaded)
export ATTEMPT_DIR="$WORK/attempt-$ATTEMPT"      # becomes gs://.../attempt-N/
export OUT_CORPUS="$ATTEMPT_DIR/corpus"
export OUT_CODE="$ATTEMPT_DIR/code"
export OUT_README="$ATTEMPT_DIR/README.txt"
export STATUS_DIR="${STATUS_DIR:-$WORK/status}"  # heartbeat json + run.log (numbers only, no identities)
export TMPDIR="${TMPDIR:-$WORK/tmp}"             # temp files stay on the disposable filesystem
export PIP_CACHE_DIR="${PIP_CACHE_DIR:-$WORK/cache/pip}"
export XDG_CACHE_HOME="${XDG_CACHE_HOME:-$WORK/cache}"
mkdir -p "$TMPDIR" 2>/dev/null || true
[ -f "${VENV:-$WORK/venv}/bin/activate" ] && . "${VENV:-$WORK/venv}/bin/activate"   # the disposable venv, when the WSL setup created one
# Workers: measured peak RSS is 0.13-0.27 GB typical and ~1.05 GB for the worst archive, so allow one worker per
# 2 GB of RAM (and never more than the CPUs). Override with JOBS=n.
if [ -z "${JOBS:-}" ]; then
  _cpu=$(nproc 2>/dev/null || echo 2)
  _gb=$(( $(awk '/^MemTotal:/{print $2}' /proc/meminfo 2>/dev/null || echo 0) / 1048576 ))
  _by_mem=$(( _gb / 2 )); [ "$_by_mem" -lt 1 ] && _by_mem=1
  JOBS=$(( _cpu < _by_mem ? _cpu : _by_mem ))
fi
export JOBS
# Free-space floor (GiB) that the run must never go below, on $WORK and on every path in DEID_GUARD_PATHS
# (the Windows drive that holds the WSL2 disk, e.g. /mnt/c). Exceeding it stops the run safely (exit code 3).
: "${MIN_FREE_GB:=25}"; export MIN_FREE_GB
# CONSUME=1 deletes each input file once its output is verified durable: peak disk ~ 1x the corpus instead of ~1.4-1.8x.
: "${CONSUME:=1}"; export CONSUME
SRC_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export SRC_ROOT PYTHONPATH="$SRC_ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
