# Shared settings for the run scripts. Source it; do not execute it.
# WORK must be a case-sensitive Linux filesystem with room for the input (~140 GB) AND the output.
# Unchanged files are hard-linked, so output needs far less than a full second copy, but input/ and out/
# must live on the SAME filesystem for hard links to work.
: "${WORK:=$PWD/work}"
: "${ATTEMPT:=1}"
export INPUT="$WORK/input"                       # official get-input.sh target
# The official input has exactly this many files; verify and the upload gate refuse any other count.
# Override only for small test corpora:  EXPECTED_FILES=20 bash scripts/20_run_pipeline.sh
: "${EXPECTED_FILES:=188722}"; export EXPECTED_FILES
export PRIVATE="$WORK/private"                   # salt, identity list, path map, defect log  (SECRET, never uploaded)
export ATTEMPT_DIR="$WORK/attempt-$ATTEMPT"      # becomes gs://.../attempt-N/
export OUT_CORPUS="$ATTEMPT_DIR/corpus"
export OUT_CODE="$ATTEMPT_DIR/code"
export OUT_README="$ATTEMPT_DIR/README.txt"
: "${JOBS:=$(nproc)}"
export JOBS
SRC_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export SRC_ROOT PYTHONPATH="$SRC_ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
