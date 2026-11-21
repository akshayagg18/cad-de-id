# Stage functions shared by 20_run_pipeline.sh and 50_orchestrate.sh. Source env.sh first.
# Every stage is safe to re-run: harvest is skipped once harvest.done exists, transform/verify use --resume semantics.
_py() { ( cd "$SRC_ROOT" && python3 -m deid "$@" ); }
_guard_args() { echo --min-free-gb "$MIN_FREE_GB"; }

stage_harvest() {
  _py harvest --input "$INPUT" --private "$PRIVATE" --jobs "$JOBS" --resume "$@"
}
stage_transform() {
  local extra=()
  [ "${CONSUME:-0}" = 1 ] && extra+=(--consume-input)
  _py transform --input "$INPUT" --output "$OUT_CORPUS" --private "$PRIVATE" --jobs "$JOBS" --resume \
      --min-free-gb "$MIN_FREE_GB" "${extra[@]}" "$@"
}
stage_verify() {
  _py verify --input "$INPUT" --output "$OUT_CORPUS" --private "$PRIVATE" --jobs "$JOBS" --expected-files "$EXPECTED_FILES"
}
stage_readme() {
  _py readme --private "$PRIVATE" --attempt "$ATTEMPT" --out "$OUT_README"
}
stage_package() { bash "$SRC_ROOT/scripts/30_package_code.sh"; }
stage_gate() {
  _py gate --output "$OUT_CORPUS" --private "$PRIVATE" --attempt-dir "$ATTEMPT_DIR" --expected-files "$EXPECTED_FILES"
}
stage_download() { bash "$SRC_ROOT/scripts/10_download.sh"; }
