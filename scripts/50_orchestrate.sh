#!/usr/bin/env bash
# One resumable run, start to finish, with a heartbeat and a disk watchdog:
#   download -> harvest -> transform -> verify -> README -> package code -> gate        (it stops BEFORE any upload)
# usage:  bash scripts/50_orchestrate.sh sample     # first LIMIT (default 100) official files, own WORK, EXPECTED_FILES=LIMIT
#         bash scripts/50_orchestrate.sh full       # all 188,722 files
# Re-running continues where it stopped (exit 3 = stopped safely because free space fell below MIN_FREE_GB).
# Writes only numbers to $STATUS_DIR: status.json, peak.json, summary.json, run.log. If MIRROR_DIR is set, a copy of the
# status files is refreshed there every HEARTBEAT_SEC (the launcher points it at the Windows pc/status folder).
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
MODE="${1:-}"
case "$MODE" in
  sample) export LIMIT="${LIMIT:-100}"; export EXPECTED_FILES="$LIMIT" ;;
  full)   unset LIMIT PREFIX ;;
  *) echo "usage: bash scripts/50_orchestrate.sh sample|full"; exit 2 ;;
esac
source "$HERE/env.sh"; source "$HERE/lib.sh"
mkdir -p "$STATUS_DIR" "$PRIVATE" "$ATTEMPT_DIR"; chmod 700 "$PRIVATE"
LOG="$STATUS_DIR/run.log"; STAGE_FILE="$STATUS_DIR/stage"; PID_FILE="$STATUS_DIR/stage.pid"
rm -f "$STATUS_DIR/DISK_LOW" "$STATUS_DIR/FAILED" "$STATUS_DIR/DONE"
say() { printf '%s  %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" | tee -a "$LOG"; }
free_gib() { df -P -k "$1" 2>/dev/null | awk 'NR==2{printf "%d", $4/1048576}'; }
guards() { echo "$WORK"; printf '%s' "${DEID_GUARD_PATHS:-}" | tr ':' '\n'; }

snapshot() {   # one heartbeat: status.json + peak.json (+ summary.json when verify has run) + optional mirror
  ( cd "$SRC_ROOT" && python3 -m deid status --private "$PRIVATE" --status-file "$STATUS_DIR/status.json" \
      --peak-file "$STATUS_DIR/peak.json" --summary-file "$STATUS_DIR/summary.json" --log "$LOG" \
      --stage "$(cat "$STAGE_FILE" 2>/dev/null || echo '?')" --msg "mode=$MODE jobs=$JOBS" ) >/dev/null 2>&1
  if [ -n "${MIRROR_DIR:-}" ] && [ -d "$MIRROR_DIR" ]; then
    cp -f "$STATUS_DIR/status.json" "$STATUS_DIR/peak.json" "$STATUS_DIR/summary.json" "$MIRROR_DIR/" 2>/dev/null
    tail -n 60 "$LOG" > "$MIRROR_DIR/run_tail.log" 2>/dev/null
    for m in DISK_LOW FAILED DONE; do [ -e "$STATUS_DIR/$m" ] && cp -f "$STATUS_DIR/$m" "$MIRROR_DIR/$m" 2>/dev/null; done
  fi
}
watchdog() {   # background: heartbeat + stop the running stage if any guarded filesystem is below the floor
  while :; do
    snapshot
    low=""
    while IFS= read -r g; do
      [ -n "$g" ] && [ -e "$g" ] || continue
      f=$(free_gib "$g"); [ -n "$f" ] && [ "$f" -lt "$MIN_FREE_GB" ] && low="$g:${f}GiB"
    done < <(guards)
    if [ -n "$low" ] && [ ! -e "$STATUS_DIR/DISK_LOW" ]; then
      echo "free space below ${MIN_FREE_GB} GiB at $low" > "$STATUS_DIR/DISK_LOW"
      say "WATCHDOG: free space below ${MIN_FREE_GB} GiB ($low) - stopping the current stage so nothing fills the disk"
      p=$(cat "$PID_FILE" 2>/dev/null); [ -n "$p" ] && { kill -TERM -- "-$p" 2>/dev/null || kill -TERM "$p" 2>/dev/null; }
    fi
    sleep "${HEARTBEAT_SEC:-60}"
  done
}
WD=""
finish() { [ -n "$WD" ] && kill "$WD" 2>/dev/null; snapshot; }
trap finish EXIT
trap 'say "interrupted - re-run the same command to resume"; exit 130' INT TERM

run_stage() {   # run_stage <label> <function> [args]  -> exit code of the stage; own process group so it can be stopped
  local name="$1" fn="$2"; shift 2
  echo "$name" > "$STAGE_FILE"; say "STAGE $name: start"
  setsid bash -c 'source "$1/env.sh"; source "$1/lib.sh"; shift; "$@"' _ "$HERE" "$fn" "$@" >> "$LOG" 2>&1 &
  echo $! > "$PID_FILE"; wait "$!"; local rc=$?
  : > "$PID_FILE"; say "STAGE $name: exit $rc"
  [ -e "$STATUS_DIR/DISK_LOW" ] && rc=3
  return $rc
}
stop_with() {   # stop_with <exit> <label> <message>
  echo "$3" > "$STATUS_DIR/$2"; say "STOPPED ($2): $3"; exit "$1"
}

say "run start: mode=$MODE jobs=$JOBS min_free=${MIN_FREE_GB}GiB consume=$CONSUME expected_files=$EXPECTED_FILES work=$WORK"
watchdog & WD=$!

# Download. Skipped once transform has started: with CONSUME=1 the input shrinks, and the downloader would fetch it again.
if [ -e "$WORK/.download_complete" ] || [ -s "$PRIVATE/path_map.tsv" ]; then
  say "download: already complete"
else
  run_stage DOWNLOAD stage_download; rc=$?
  [ $rc = 3 ] && stop_with 3 DISK_LOW "stopped safely while downloading; free space and re-run to resume"
  [ $rc != 0 ] && stop_with 1 FAILED "download did not finish (exit $rc); re-run to resume"
  date -u +%FT%TZ > "$WORK/.download_complete"
fi
run_stage HARVEST   stage_harvest;   rc=$?; [ $rc = 3 ] && stop_with 3 DISK_LOW "stopped safely in harvest"; [ $rc != 0 ] && stop_with 1 FAILED "harvest failed (exit $rc)"
run_stage TRANSFORM stage_transform; rc=$?; [ $rc = 3 ] && stop_with 3 DISK_LOW "stopped safely in transform; re-run to resume"; [ $rc != 0 ] && stop_with 1 FAILED "transform failed (exit $rc)"
run_stage VERIFY    stage_verify;    rc=$?; [ $rc != 0 ] && stop_with 1 FAILED "verify FAILED (exit $rc) - not uploading; see $PRIVATE/verify_report.json"
run_stage README    stage_readme;    rc=$?; [ $rc != 0 ] && stop_with 1 FAILED "readme failed (exit $rc)"
run_stage PACKAGE   stage_package;   rc=$?; [ $rc != 0 ] && stop_with 1 FAILED "packaging code failed (exit $rc)"
run_stage GATE      stage_gate;      rc=$?; [ $rc != 0 ] && stop_with 1 FAILED "upload gate FAILED (exit $rc) - not uploading"
echo "gate passed; attempt ready at $ATTEMPT_DIR" > "$STATUS_DIR/DONE"
echo READY > "$STAGE_FILE"; say "ALL STAGES DONE: $ATTEMPT_DIR is verified and gated (nothing was uploaded)"
exit 0
