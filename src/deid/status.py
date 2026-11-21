"""Tiny progress/heartbeat file so a long unattended run can be followed (and debugged) from outside the VM.
Contains only counts, free space and the last log line - never identities, tokens or file contents."""
from __future__ import annotations

import json
import os
import shutil
import time


def _free(path: str):
    try:
        return round(shutil.disk_usage(path).free / (1 << 30), 1)
    except OSError:
        return None


def _lines(path: str) -> int:
    try:
        with open(path, "rb") as f:
            return sum(1 for _ in f)
    except OSError:
        return 0


def _count(root: str, skip_top=()) -> int:
    n = 0
    for r, dirs, files in os.walk(root):
        if r == root and skip_top:
            dirs[:] = [x for x in dirs if x not in skip_top]
        n += len(files)
    return n


def _used(path: str):
    try:
        return round(shutil.disk_usage(path).used / (1 << 30), 1)
    except OSError:
        return None


def _last_line(path: str) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 4096))
            tail = f.read().decode("utf-8", "replace").strip().splitlines()
        return tail[-1][:300] if tail else ""
    except OSError:
        return ""


def collect(stage: str, msg: str, private: str, work: str | None = None, guard_paths=(), log: str | None = None,
            input_dir: str | None = None, output_dir: str | None = None) -> dict:
    d = {"time_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "stage": stage, "message": msg[:300]}
    if work:
        d["free_gib_work"] = _free(work)
    for g in guard_paths:
        if g and os.path.exists(g):
            d[f"free_gib_host[{g}]"] = _free(g)
    if work:
        d["used_gib_work"] = _used(work)
    d["path_map_rows"] = _lines(os.path.join(private, "path_map.tsv"))
    d["inputs_consumed"] = _lines(os.path.join(private, "consumed.tsv"))
    if input_dir and os.path.isdir(input_dir):
        d["input_files_now"] = _count(input_dir, skip_top=(".get-input",))
    if output_dir and os.path.isdir(output_dir):
        d["output_files_now"] = _count(output_dir)
    if log:
        d["last_log_line"] = _last_line(log)
    return d


def write(out: str, data: dict) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    tmp = out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, out)


def update_peak(peak_file: str, data: dict) -> dict:
    """Keep the extremes seen over a whole run: lowest free space (work fs and each guarded host drive) and the
    highest used space on the work filesystem. Numbers only; safe to share."""
    try:
        with open(peak_file, encoding="utf-8") as f:
            peak = json.load(f)
    except (OSError, ValueError):
        peak = {}
    for k, v in data.items():
        if v is None or not (k.startswith("free_gib_") or k == "used_gib_work"):
            continue
        name = "min_" + k if k.startswith("free_gib_") else "max_" + k
        old = peak.get(name)
        peak[name] = v if old is None else (min(old, v) if name.startswith("min_") else max(old, v))
    peak["samples"] = int(peak.get("samples", 0)) + 1
    peak["last_utc"] = data.get("time_utc")
    write(peak_file, peak)
    return peak


def summarize(private: str) -> dict:
    """Numbers-only digest of a finished run (no paths, no identities, no sample lists): safe to copy out of the private
    directory, to a report, or into shared notes."""
    def load(name):
        try:
            with open(os.path.join(private, name), encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}
    rep, ts, hs = load("verify_report.json"), load("transform_stats.json"), load("harvest_stats.json")
    leak = rep.get("leak_scan", {}) if isinstance(rep.get("leak_scan"), dict) else {}
    out = {"verify_ok": rep.get("ok"), "checks": rep.get("checks", {}),
           "inputs": rep.get("inputs"), "outputs": rep.get("outputs"), "mapped": rep.get("mapped"),
           "inputs_consumed": rep.get("inputs_consumed"), "action_counts": rep.get("action_counts", {}),
           "warnings": len(rep.get("warnings", [])), "generated_utc": rep.get("generated_utc"),
           "leak_scan": {k: v for k, v in leak.items() if isinstance(v, (int, float, bool))},
           "transform_stats": {k: v for k, v in ts.get("stats", ts).items() if isinstance(v, (int, float))} if isinstance(ts, dict) else {},
           "harvest": {"wall_seconds": hs.get("wall_seconds"), "files": hs.get("files")}}
    return out
