"""Orchestration: harvest -> transform -> verify -> README. Everything private lives under --private."""
from __future__ import annotations

import argparse
import collections
import io
import json
import multiprocessing as mp
import os
import sys
import time

from . import diskguard
from . import status as status_mod
from . import harvest as H
from .identity import IdentityStore, Replacer, load_or_create_salt
from .rewriters import Ctx, TMP_SUFFIX, classify, process_blob, process_file, _ext

SKIP_TOP = {".get-input"}


# ------------------------------------------------------------------------------------------
def walk_inputs(input_dir: str):
    """Relative '/'-separated paths of every regular file below input_dir (sorted, deterministic)."""
    out = []
    for root, dirs, files in os.walk(input_dir):
        if root == input_dir:
            dirs[:] = sorted(d for d in dirs if d not in SKIP_TOP)
        else:
            dirs.sort()
        for f in sorted(files):
            p = os.path.join(root, f)
            if os.path.islink(p) or not os.path.isfile(p):
                continue
            out.append(os.path.relpath(p, input_dir).replace(os.sep, "/"))
    return out


# ------------------------------------------------------------------------------------------
# Phase 1
# ------------------------------------------------------------------------------------------
_G: dict = {}


def _h_init(input_dir: str, deep: bool):
    _G["input"], _G["deep"] = input_dir, deep


def _ext_of(rel: str) -> str:
    base = rel.rsplit("/", 1)[-1]
    return base.rsplit(".", 1)[-1].lower() if "." in base else "(none)"


def _h_one(rel: str):
    p = os.path.join(_G["input"], rel)
    t = time.perf_counter()
    found = H.harvest_file(p, rel, _G["deep"])
    dt = time.perf_counter() - t
    try:
        size = os.path.getsize(p)
    except OSError:
        size = 0
    return found, _ext_of(rel), size, dt


def run_harvest(input_dir: str, private: str, jobs: int, deep: bool = True, files: list | None = None) -> IdentityStore:
    salt = load_or_create_salt(os.path.join(private, "salt.key"))
    store = IdentityStore(salt)
    rels = files if files is not None else walk_inputs(input_dir)
    t0 = time.time()
    per_ext = collections.defaultdict(lambda: [0, 0, 0.0])        # ext -> [files, bytes, cpu seconds]
    for rel in rels:                                   # folder-layout identities (cheap)
        for kind, val in H.harvest_path(rel):
            store.add(kind, val)
    with mp.Pool(jobs, initializer=_h_init, initargs=(input_dir, deep)) as pool:
        for i, (found, ext, size, dt) in enumerate(pool.imap_unordered(_h_one, rels, chunksize=16), 1):
            pe = per_ext[ext]
            pe[0] += 1; pe[1] += size; pe[2] += dt
            for kind, val in found:
                store.add(kind, val)
            if i % 5000 == 0:
                print(f"[harvest] {i}/{len(rels)} files, {len(store.phrases)} phrases, {time.time()-t0:.0f}s", flush=True)
    store.finalize()
    store.save(os.path.join(private, "identities.json"))
    with open(os.path.join(private, "harvest.done"), "w") as f:      # written last: "identities.json is complete"
        json.dump({"files": len(rels), "phrases": len(store.phrases)}, f)
    with open(os.path.join(private, "harvest_stats.json"), "w") as f:
        json.dump({"wall_seconds": round(time.time() - t0, 1), "files": len(rels),
                   "per_ext": {k: v for k, v in sorted(per_ext.items(), key=lambda kv: -kv[1][2])}}, f, indent=1)
    kinds = collections.Counter(store.phrases.values())
    print(f"[harvest] done: {len(store.phrases)} phrases {dict(kinds)}", flush=True)
    return store


# ------------------------------------------------------------------------------------------
# Phase 2
# ------------------------------------------------------------------------------------------
def _t_init(input_dir, output_dir, private, link, rename_owners, rename_paths=True):
    salt = load_or_create_salt(os.path.join(private, "salt.key"))
    store = IdentityStore.load(os.path.join(private, "identities.json"), salt)
    rep = Replacer(store)
    _G.update(input=input_dir, output=output_dir, link=link, rename=rename_owners, rename_paths=rename_paths,
              ctx=Ctx(rep=rep, pz=store.pz, rename_owners=rename_owners), rep=rep)


def _t_one(rel: str):
    ctx: Ctx = _G["ctx"]
    rep: Replacer = _G["rep"]
    ctx.defects.clear()
    before = dict(ctx.stats)
    rel_out = rep.map_path(rel, rename_owners=_G["rename"]) if _G["rename_paths"] else rel
    src = os.path.join(_G["input"], rel)
    dst = os.path.join(_G["output"], rel_out)
    t_start = time.perf_counter()
    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        action = process_file(src, dst, rel, ctx, link=_G["link"])
        ext = _ext_of(rel)
        ctx.bump("n." + ext)
        ctx.bump("bytes." + ext, os.path.getsize(src))
        ctx.bump("us." + ext, int((time.perf_counter() - t_start) * 1e6))
    except Exception as e:
        action = "ERROR"
        ctx.defect(rel, f"unexpected failure {type(e).__name__}: {str(e)[:100]}")
    defects = []
    for where, why in ctx.defects:
        top, _, rest = where.partition("!")
        ftop = rep.map_path(top, rename_owners=_G["rename"]) if _G["rename_paths"] else top
        defects.append((ftop + (("!" + rest) if rest else ""), why))
    delta = {k: v - before.get(k, 0) for k, v in ctx.stats.items() if v != before.get(k, 0)}
    return rel, rel_out, action, os.path.getsize(src), defects, delta


def sweep_tmp(output_dir):
    n = 0
    for root, _d, files in os.walk(output_dir):
        for name in files:
            if name.endswith(TMP_SUFFIX):
                os.unlink(os.path.join(root, name)); n += 1
    return n


def _resume_state(map_path: str, defects_path: str, output_dir: str, rels: set) -> tuple[set, int, list]:
    """Decide which recorded rows can be trusted on --resume.

    A row counts as done only if its output file still exists (and, for byte-identical files, still has the
    recorded size) and it did not end in ERROR. Every other row is dropped from path_map.tsv (rewritten
    atomically) together with the defect lines that belong to it, so the file is processed again and no
    duplicate rows can appear. Returns (done input paths, number of dropped rows).
    """
    kept: list[str] = []
    done: set = set()
    rewritten: list = []
    dropped_outs: set = set()
    dropped = 0
    with open(map_path, encoding="utf-8") as f:
        for line in f:
            parts = line.rstrip("\n").split("\t")
            if len(parts) != 4:                                   # torn last line of a crashed run
                dropped += 1
                continue
            rel, rel_out, action, size = parts
            p = os.path.join(output_dir, rel_out)
            ok = rel in rels and rel not in done and action != "ERROR" and os.path.isfile(p) and not os.path.islink(p)
            if ok and action == "unchanged":
                try:
                    ok = os.path.getsize(p) == int(size)
                except (OSError, ValueError):
                    ok = False
            if ok:
                kept.append(line if line.endswith("\n") else line + "\n")
                done.add(rel)
                if action == "rewritten":
                    rewritten.append(rel)
            else:
                dropped += 1
                dropped_outs.add(rel_out)
    tmp = map_path + TMP_SUFFIX
    with open(tmp, "w", encoding="utf-8") as f:
        f.writelines(kept)
    os.replace(tmp, map_path)
    if os.path.exists(defects_path):                              # forget defects of rows that will be redone
        seen, keep_d = set(), []
        with open(defects_path, encoding="utf-8") as f:
            for line in f:
                where = line.split("\t", 1)[0].partition("!")[0]
                if where in dropped_outs or line in seen:
                    continue
                seen.add(line)
                keep_d.append(line if line.endswith("\n") else line + "\n")
        tmp = defects_path + TMP_SUFFIX
        with open(tmp, "w", encoding="utf-8") as f:
            f.writelines(keep_d)
        os.replace(tmp, defects_path)
    return done, dropped, rewritten


def _load_consumed(private: str) -> set:
    p = os.path.join(private, "consumed.tsv")
    if not os.path.exists(p):
        return set()
    with open(p, encoding="utf-8") as f:
        return {l.rstrip("\n") for l in f if l.strip()}


def _consume(files, pending: list, input_dir: str, conf) -> None:
    """Delete the input copy of files that were rewritten. Order matters for crash safety: the path_map rows are made
    durable first, the intent is logged to consumed.tsv and made durable, only then are the inputs unlinked."""
    if not pending:
        return
    for f in files:
        f.flush()
        os.fsync(f.fileno())
    for rel in pending:
        conf.write(rel + "\n")
    conf.flush()
    os.fsync(conf.fileno())
    for rel in pending:
        try:
            os.unlink(os.path.join(input_dir, rel))
        except FileNotFoundError:
            pass
    pending.clear()


def run_transform(input_dir, output_dir, private, jobs, link=True, rename_owners=True, resume=False, rename_paths=True,
                  consume_input=False, min_free_gib=0.0, guard_paths=None, guard_every=100):
    """Write one output per input.

    consume_input: low-disk mode. As soon as a file has been REWRITTEN (and its path_map row is durable) the input copy
        is deleted, so the disk never holds both versions. Byte-identical files are hard links and cost nothing extra.
        The original corpus then has to be downloaded again for another attempt. Needs phase 1 (harvest) to be done.
    min_free_gib: raise diskguard.DiskLow (after a clean stop) when a guarded filesystem drops below this.
    """
    os.makedirs(output_dir, exist_ok=True)
    os.makedirs(private, exist_ok=True)
    if consume_input and not os.path.exists(os.path.join(private, "identities.json")):
        raise SystemExit("--consume-input needs phase 1 first: run `python -m deid harvest` (identities.json missing)")
    rels = walk_inputs(input_dir)
    done = set()
    map_path = os.path.join(private, "path_map.tsv")
    defects_path = os.path.join(private, "defects.tsv")
    consumed_path = os.path.join(private, "consumed.tsv")
    consumed = _load_consumed(private) if resume else set()
    if not resume and os.path.exists(consumed_path):
        os.unlink(consumed_path)                                    # a fresh run starts from a complete input
    if resume and os.path.exists(map_path):
        done, dropped, rewritten_done = _resume_state(map_path, defects_path, output_dir, set(rels) | consumed)
        if dropped:
            print(f"[transform] resume: {dropped} recorded rows had a missing/changed output or failed earlier "
                  f"and will be processed again", flush=True)
        lost = [r for r in consumed if r not in done and not os.path.exists(os.path.join(input_dir, r))]
        if lost:
            print(f"[transform] WARNING: {len(lost)} consumed input(s) have no valid output any more and cannot be redone "
                  f"without downloading the corpus again (first: {lost[0]})", flush=True)
        if consume_input:                                           # finish deletions interrupted by a crash
            leftovers = [r for r in rewritten_done if os.path.exists(os.path.join(input_dir, r))]
            with open(consumed_path, "a", encoding="utf-8") as conf:
                _consume([], leftovers, input_dir, conf)
    todo = [r for r in rels if r not in done]
    print(f"[transform] {len(rels)} inputs, {len(done)} already done, {len(todo)} to do"
          f"{' (consume-input: ON)' if consume_input else ''}", flush=True)
    stats = collections.Counter()
    actions = collections.Counter()
    t0 = time.time()
    mode = "a" if resume else "w"               # a fresh run must never inherit rows from an earlier run
    guard = guard_paths if guard_paths is not None else diskguard.default_paths(output_dir)
    pending: list = []
    try:
        with open(map_path, mode, encoding="utf-8") as mapf, open(defects_path, mode, encoding="utf-8") as deff, \
                open(consumed_path, "a", encoding="utf-8") as conf, \
                mp.Pool(jobs, initializer=_t_init, initargs=(input_dir, output_dir, private, link, rename_owners, rename_paths)) as pool:
            for i, (rel, rel_out, action, size, defects, delta) in enumerate(pool.imap_unordered(_t_one, todo, chunksize=8), 1):
                mapf.write(f"{rel}\t{rel_out}\t{action}\t{size}\n")
                for where, why in defects:
                    deff.write(f"{where}\t{why}\n")
                actions[action] += 1
                stats.update(delta)
                if consume_input and action == "rewritten":
                    pending.append(rel)
                if i % guard_every == 0:
                    if consume_input:
                        _consume([mapf, deff], pending, input_dir, conf)
                    if min_free_gib > 0:
                        diskguard.check(guard, min_free_gib)        # raises DiskLow; the with-block terminates the pool
                if i % 2000 == 0:
                    mapf.flush(); deff.flush()
                    print(f"[transform] {i}/{len(todo)} {dict(actions)} {time.time()-t0:.0f}s", flush=True)
            if consume_input:
                _consume([mapf, deff], pending, input_dir, conf)
    except diskguard.DiskLow as e:
        swept = sweep_tmp(output_dir)
        print(f"[transform] STOPPED SAFELY: {e}. {sum(actions.values())} files finished in this run, {swept} partial temp "
              f"file(s) removed. Free some space and run again with --resume.", flush=True)
        raise
    swept = sweep_tmp(output_dir)
    if swept:
        print(f"[transform] removed {swept} stale temp files", flush=True)
    print(f"[transform] done {dict(actions)} stats={dict(stats)}", flush=True)
    with open(os.path.join(private, "transform_stats.json"), "w") as f:
        json.dump({"actions": actions, "stats": stats}, f, indent=1)
    return actions, stats


# ------------------------------------------------------------------------------------------
# Verification
# ------------------------------------------------------------------------------------------
def _views(name: str, data: bytes, depth: int = 0):
    """Yield text views of the zones that carry identities (for the leak scan)."""
    kind = classify(name, data[:4096])
    if kind in ("step", "iges", "sat", "stl"):
        yield data[: 1 << 20].decode("latin-1")
        if kind == "step":
            lo = 0
            while True:
                p = data.find(b"PERSON(", lo)
                q = data.find(b"ORGANIZATION(", lo)
                cand = [x for x in (p, q) if x >= 0]
                if not cand:
                    break
                s = min(cand)
                yield data[max(0, s - 12): s + 400].decode("latin-1")
                lo = s + 8
    elif kind == "text":
        yield data[:32 << 20].decode("latin-1")
    elif kind == "ole":
        import olefile
        ole = olefile.OleFileIO(io.BytesIO(data))
        try:
            for e in ole.listdir(streams=True, storages=False):
                if e[-1].startswith("\x05"):
                    raw = ole.openstream(e).read()
                    yield raw.decode("latin-1")
                    yield raw.decode("utf-16-le", "ignore")
        finally:
            ole.close()
    elif kind == "pdf":
        import pikepdf
        with pikepdf.open(io.BytesIO(data)) as pdf:
            for k in pdf.docinfo.keys():
                yield str(pdf.docinfo[k])
            md = pdf.Root.get("/Metadata")
            if md is not None:
                yield bytes(md.read_bytes()).decode("utf-8", "ignore")
    elif kind == "zip" and depth < 3:
        import zipfile
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for info in z.infolist():
                yield info.filename
                if not info.is_dir() and info.file_size < (64 << 20):
                    yield from _views(info.filename, z.read(info), depth + 1)
    elif kind == "gzip" and depth < 3:
        import gzip
        yield from _views(name[:-3], gzip.decompress(data), depth + 1)
    elif kind == "tar" and depth < 3:
        import tarfile
        with tarfile.open(fileobj=io.BytesIO(data)) as t:
            for m in t:
                yield m.name
                yield m.uname or ""
                if m.isfile() and m.size < (64 << 20):
                    yield from _views(m.name, t.extractfile(m).read(), depth + 1)


def _v_init(private):
    salt = load_or_create_salt(os.path.join(private, "salt.key"))
    store = IdentityStore.load(os.path.join(private, "identities.json"), salt)
    _G["rep"] = Replacer(store)


def _v_one(args):
    rel_out, path = args
    rep: Replacer = _G["rep"]
    rep.hits.clear()
    leaks = 0
    # path itself
    rep._phrases(rel_out)
    leaks += sum(rep.hits.values())
    try:
        size = os.path.getsize(path)
        if 0 < size <= (300 << 20):
            with open(path, "rb") as f:
                data = f.read()
            for v in _views(rel_out, data):
                rep.hits.clear()
                rep._phrases(v)
                leaks += sum(rep.hits.values())
    except Exception:
        return rel_out, leaks, "unreadable"
    return rel_out, leaks, "ok"


def run_verify(input_dir, output_dir, private, jobs, expected_files: int | None = None) -> dict:
    rep: dict = {"checks": {}, "output_dir": os.path.realpath(output_dir),
                 "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    consumed = _load_consumed(private)                 # inputs deleted by --consume-input still count as inputs
    ins = set(walk_inputs(input_dir)) | consumed
    outs = set(walk_inputs(output_dir))
    rows = []
    with open(os.path.join(private, "path_map.tsv"), encoding="utf-8") as f:
        for line in f:
            rel, rel_out, action, size = line.rstrip("\n").split("\t")
            rows.append((rel, rel_out, action, int(size)))
    mapped_in = {r[0] for r in rows}
    out_names = [r[1] for r in rows]
    rep["inputs"], rep["outputs"], rep["mapped"] = len(ins), len(outs), len(mapped_in)
    rep["inputs_consumed"] = len(consumed)
    if expected_files is not None:
        rep["expected_files"] = expected_files
        rep["checks"]["input_count_is_expected"] = len(ins) == expected_files
        rep["checks"]["output_count_is_expected"] = len(outs) == expected_files
    rep["checks"]["no_failed_files"] = not any(r[2] == "ERROR" for r in rows)
    rep["checks"]["every_input_mapped"] = ins == mapped_in
    rep["checks"]["output_count_equals_input"] = len(outs) == len(ins)
    rep["checks"]["no_output_name_collisions"] = len(set(out_names)) == len(out_names)
    rep["checks"]["all_mapped_outputs_exist"] = set(out_names) == outs
    bad_ext = [r for r in rows if _ext(r[0]) != _ext(r[1])]
    rep["checks"]["extensions_preserved"] = not bad_ext
    size_bad = []
    for rel, rel_out, action, size in rows:
        p = os.path.join(output_dir, rel_out)
        if os.path.exists(p) and action in ("unchanged",) and os.path.getsize(p) != size:
            size_bad.append(rel_out)
    rep["checks"]["unchanged_files_same_size"] = not size_bad
    zero_in = sum(1 for r in rows if r[3] == 0)
    zero_out = sum(1 for r in rows if os.path.exists(os.path.join(output_dir, r[1])) and os.path.getsize(os.path.join(output_dir, r[1])) == 0)
    rep["checks"]["empty_files_preserved"] = zero_in == zero_out
    rep["action_counts"] = dict(collections.Counter(r[2] for r in rows))
    # leak scan. A file whose identity-bearing zones cannot be parsed was NOT scanned. If the pipeline recorded a defect
    # for it (corrupt source, unsupported container, ...) it is a known source defect and only counted. Without a defect
    # record it is unexplained: for a REWRITTEN output that is new corruption (hard failure), for a byte-identical file
    # it is a warning that something went unscanned. Nothing is silently ignored.
    leaks_total, leak_files = 0, []
    unreadable = []
    action_of = {r[1]: r[2] for r in rows}
    tasks = [(r[1], os.path.join(output_dir, r[1])) for r in rows]
    with mp.Pool(jobs, initializer=_v_init, initargs=(private,)) as pool:
        for rel_out, leaks, status in pool.imap_unordered(_v_one, tasks, chunksize=32):
            if leaks:
                leaks_total += leaks
                leak_files.append(rel_out)
            if status != "ok":
                unreadable.append(rel_out)
    recorded = set()
    dpath = os.path.join(private, "defects.tsv")
    if os.path.exists(dpath):
        with open(dpath, encoding="utf-8") as f:
            for line in f:
                recorded.add(line.split("\t", 1)[0].partition("!")[0])
    known = sorted(x for x in unreadable if x in recorded)
    bad_rewritten = sorted(x for x in unreadable if x not in recorded and action_of.get(x) == "rewritten")
    bad_unchanged = sorted(x for x in unreadable if x not in recorded and action_of.get(x) != "rewritten")
    rep["leak_scan"] = {"phrase_hits_in_output": leaks_total, "files_with_hits": len(leak_files),
                        "sample_files": sorted(leak_files)[:20],
                        "unreadable_views": len(unreadable),
                        "unreadable_known_source_defects": len(known),
                        "unreadable_rewritten_unexplained": len(bad_rewritten),
                        "unreadable_rewritten_unexplained_sample": bad_rewritten[:20],
                        "unreadable_unchanged_unexplained": len(bad_unchanged),
                        "unreadable_unchanged_unexplained_sample": bad_unchanged[:20]}
    rep["checks"]["no_harvested_identity_in_output"] = leaks_total == 0
    rep["checks"]["rewritten_outputs_readable"] = not bad_rewritten
    rep["warnings"] = []
    if bad_unchanged:
        rep["warnings"].append(f"{len(bad_unchanged)} byte-identical output file(s) could not be parsed for the leak scan "
                               f"and have no defect record - inspect leak_scan.unreadable_unchanged_unexplained_sample")
    rep["ok"] = all(rep["checks"].values())
    with open(os.path.join(private, "verify_report.json"), "w") as f:
        json.dump(rep, f, indent=1)
    return rep


# ------------------------------------------------------------------------------------------
# Pre-upload gate
# ------------------------------------------------------------------------------------------
GATE_TOP = {"corpus", "code", "README.txt"}
PRIVATE_NAMES = {"salt.key", "identities.json", "path_map.tsv", "defects.tsv", "verify_report.json",
                 "harvest_stats.json", "transform_stats.json"}


def run_gate(private: str, corpus_dir: str, attempt_dir: str, expected_files: int | None = None,
             max_objects: int = 400_000) -> tuple[bool, list, list]:
    """Everything that must hold before attempt-N/ may be uploaded. Returns (ok, errors, notes)."""
    errors: list = []
    notes: list = []
    rp = os.path.join(private, "verify_report.json")
    rep = None
    if not os.path.isfile(rp):
        errors.append("verify_report.json is missing - run `python -m deid verify` first")
    else:
        with open(rp, encoding="utf-8") as f:
            rep = json.load(f)
        failed = sorted(k for k, v in rep.get("checks", {}).items() if not v)
        if rep.get("ok") is not True or failed:
            errors.append(f"verify_report.json is not ok (failed checks: {', '.join(failed) or 'unknown'})")
        if expected_files is not None:
            for k in ("inputs", "outputs", "mapped"):
                if rep.get(k) != expected_files:
                    errors.append(f"verify_report {k}={rep.get(k)} but {expected_files} files are expected")
        if os.path.realpath(rep.get("output_dir", "")) != os.path.realpath(corpus_dir):
            errors.append("verify_report.json was produced for a different output directory")
        mp_ = os.path.join(private, "path_map.tsv")
        if os.path.isfile(mp_) and os.path.getmtime(mp_) > os.path.getmtime(rp) + 1:
            errors.append("path_map.tsv is newer than verify_report.json - transform ran after verify; re-run verify")
        if rep.get("action_counts", {}).get("ERROR", 0):
            errors.append(f"{rep['action_counts']['ERROR']} file(s) ended in ERROR")
        notes.extend(rep.get("warnings", []))
        ls = rep.get("leak_scan", {})
        notes.append(f"known source defects that could not be scanned: {ls.get('unreadable_known_source_defects', 0)}")
    n = 0
    big = 0
    for root, dirs, files in os.walk(corpus_dir):
        for d in dirs:
            if os.path.islink(os.path.join(root, d)):
                errors.append(f"symlinked directory in corpus: {os.path.join(root, d)}")
        for f in files:
            fp = os.path.join(root, f)
            if os.path.islink(fp):
                errors.append(f"symlink in corpus (the uploader skips it): {fp}")
                continue
            if f.endswith(TMP_SUFFIX):
                errors.append(f"leftover temp file in corpus: {fp}")
            if os.path.getsize(fp) > (2 << 30):
                big += 1
            n += 1
        if len(errors) > 50:
            break
    target = expected_files if expected_files is not None else (rep or {}).get("outputs")
    if target is not None and n != target:
        errors.append(f"corpus/ holds {n} files, expected {target}")
    if n > max_objects:
        errors.append(f"{n} corpus files exceed the {max_objects} object limit")
    if big:
        notes.append(f"{big} file(s) over 2 GB are skipped by the scorer")
    try:
        top = set(os.listdir(attempt_dir))
    except OSError:
        top = set()
        errors.append(f"attempt directory {attempt_dir} not found")
    if top:
        for extra in sorted(top - GATE_TOP):
            errors.append(f"unexpected entry in the attempt directory (only corpus/, code/, README.txt belong there): {extra}")
        for need in sorted(GATE_TOP - top):
            errors.append(f"attempt directory lacks {need}")
    rd = os.path.join(attempt_dir, "README.txt")
    if os.path.isfile(rd) and os.path.getsize(rd) == 0:
        errors.append("README.txt is empty")
    code = os.path.join(attempt_dir, "code")
    if os.path.isdir(code):
        if not os.path.isfile(os.path.join(code, "src", "deid", "__init__.py")):
            errors.append("code/ does not contain the pipeline source (src/deid)")
        for root, _dirs, files in os.walk(code):
            for f in files:
                if f in PRIVATE_NAMES or f.endswith((".tsv", ".token", ".env")) or f.startswith("salt"):
                    errors.append(f"private-looking file inside code/: {os.path.join(root, f)}")
    ap = os.path.realpath(attempt_dir) + os.sep
    if (os.path.realpath(private) + os.sep).startswith(ap):
        errors.append("the private directory lives inside the attempt directory")
    notes.append(f"corpus files: {n}")
    return (not errors), errors, notes


# ------------------------------------------------------------------------------------------
# README.txt for the submission (final paths / neutral descriptions only)
# ------------------------------------------------------------------------------------------
def write_readme(private: str, out_path: str, attempt: int, notes: str = "") -> None:
    defects = collections.defaultdict(list)
    p = os.path.join(private, "defects.tsv")
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            for line in f:
                where, _, why = line.rstrip("\n").partition("\t")
                defects[why.split(" (")[0] if " (" in why else why].append(where)
    stats = {}
    sp = os.path.join(private, "transform_stats.json")
    if os.path.exists(sp):
        stats = json.load(open(sp))
    L = []
    L.append(f"MACHINA CAD DE-ID - attempt {attempt} - pipeline README")
    L.append("=" * 72)
    L.append("""
RUN INSTRUCTIONS (Linux, Python 3.10+; dependencies in code/requirements.txt)
  1. bash get-input.sh --jobs 16               (official downloader, into ./input/, case-sensitive FS)
  2. python -m deid harvest   --input input --private private
  3. python -m deid transform --input input --output out/corpus --private private --jobs N
  4. python -m deid verify    --input input --output out/corpus --private private
  5. python -m deid readme    --private private --attempt N --out out/README.txt
  6. python upload.py --dest gs://.../attempt-N/ --corpus out/corpus --code code --readme out/README.txt
  (code/scripts/*.sh wraps these steps.)  `private/` holds the secret salt, the identity list and the
  path map; it is never uploaded.

WHAT THE PIPELINE DOES
  - Identity harvest from identity-bearing fields only: STEP FILE_NAME author/organization/authorization and
    PERSON/ORGANIZATION entities, IGES global author/organization, OLE summary properties (author, last saved by,
    manager, company) of SolidWorks/Inventor files, PDF /Author, FreeCAD/3MF creator attributes, and GitHub/GitLab
    owner names in the folder layout. Placeholders ('user', 'Company', ...) and software vendors are kept.
  - Deterministic keyed pseudonyms (HMAC-SHA256 with a private salt). Same identity -> same pseudonym in every
    file/format/folder name/archive member; different identities -> different pseudonyms (collision-free for
    harvested names). Pseudonyms are LENGTH-PRESERVING and keep the case pattern, so no offset, size or fixed-width
    field changes (STEP, IGES Hollerith counts, OLE property sizes).
  - Structural identifiers anywhere in text zones: e-mail addresses, home-directory user names
    (C:\\Users\\<name>\\, /home/<name>/), GitHub/GitLab owner URLs.
  - File and folder names: owner part of sources/github|gitlab/<owner>__<repo>... and archives/<owner>__... plus
    any harvested identity phrase inside any path component. Extensions and all other path parts are unchanged.
  - Archives (zip/FCStd/3MF/tar/gz, nested up to depth 4) are rewritten member by member: names mapped, contents
    rewritten recursively, tar uid/gid/uname/gname cleared, gzip file name/mtime cleared.
  - Files that need no change are hard-linked/copied byte for byte. No file is dropped; every input file has
    exactly one output file.
""")
    L.append("LIMITATIONS (not handled in this attempt)")
    L.append("""  - Visible page text, embedded images/logos and PDF attachments are not rewritten.
  - STEP PRODUCT / part names and numbers, IGES D/P sections and SAT entity records are not rewritten
    (assembly/BOM links therefore stay valid byte for byte).
  - Native SolidWorks/Inventor files: only standard OLE property sets are rewritten; proprietary model streams
    and non-OLE (section-archive) containers are copied unchanged.
  - 7z / rar / Unix-compress (.z) archives and files over 300 MB (non-STEP) are copied unchanged.
  - Image EXIF/XMP is not rewritten.
""")
    L.append("EXCEPTIONS / SOURCE DEFECTS (final paths or neutral descriptions)")
    if not defects:
        L.append("  (none recorded)")
    for why, items in sorted(defects.items(), key=lambda kv: -len(kv[1])):
        L.append(f"  * {why}: {len(items)} item(s)")
        for it in items[:40]:
            L.append(f"      {it}")
        if len(items) > 40:
            L.append(f"      ... and {len(items) - 40} more of the same kind")
    L.append("")
    L.append("SOURCE-DATA NOTES")
    L.append("  - The input contains ~40k byte-identical duplicate files (kept, mapped independently), 24 empty files,")
    L.append("    '*.resume-partial' truncated downloads and pre-extracted '*.extracted' folders next to their archives;")
    L.append("    all were kept at their (renamed) final paths.")
    L.append("  - Output actions: " + json.dumps(stats.get("actions", {})))
    if notes:
        L.append("")
        L.append(notes)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")


# ------------------------------------------------------------------------------------------
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="deid", description="Machina CAD De-ID pipeline")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("harvest", "transform", "verify", "readme", "gate", "status", "all"):
        p = sub.add_parser(name)
        p.add_argument("--input", default="input")
        p.add_argument("--output", default="out/corpus")
        p.add_argument("--private", default="private")
        p.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 2)))
        p.add_argument("--no-link", action="store_true", help="copy unchanged files instead of hard-linking")
        p.add_argument("--no-rename-owners", action="store_true", help="do not apply the <owner>__<repo> folder rule")
        p.add_argument("--no-rename-paths", action="store_true", help="keep ALL file/folder names (content-only run)")
        p.add_argument("--no-deep-step", action="store_true", help="harvest STEP headers only")
        p.add_argument("--resume", action="store_true")
        p.add_argument("--attempt", type=int, default=1)
        p.add_argument("--out", default="out/README.txt")
        p.add_argument("--notes", default="")
        p.add_argument("--consume-input", action="store_true",
                       help="low-disk mode: delete each input file right after its rewritten output is durable (needs a re-download for another attempt)")
        p.add_argument("--stage", default="", help="status: current stage name")
        p.add_argument("--msg", default="", help="status: short message")
        p.add_argument("--log", default=None, help="status: log file whose last line is reported")
        p.add_argument("--status-file", default=None, help="status: JSON file to write")
        p.add_argument("--summary-file", default=None, help="status: also write the numbers-only run digest here")
        p.add_argument("--peak-file", default=None, help="status: JSON file that keeps the lowest free / highest used space seen")
        p.add_argument("--count-files", action="store_true", help="status: also count files in --input/--output (slow on 188k files)")
        p.add_argument("--guard-every", type=int, default=100, help="check free space after every N finished files")
        p.add_argument("--min-free-gb", type=float, default=0.0,
                       help="stop safely (resumable) when output filesystem or any DEID_GUARD_PATHS drive has less free space (GiB)")
        p.add_argument("--expected-files", type=int, default=None, help="exact number of input/output files (official: 188722)")
        p.add_argument("--attempt-dir", default=None, help="attempt-N directory holding corpus/, code/, README.txt (gate)")
    a = ap.parse_args(argv)
    verify_failed = False
    if a.cmd in ("harvest", "all"):
        if a.resume and os.path.exists(os.path.join(a.private, "harvest.done")) \
                and os.path.exists(os.path.join(a.private, "identities.json")):
            # Never re-harvest on resume: with --consume-input some inputs are gone, and a second harvest would
            # build a different identity registry than the files already written were pseudonymized with.
            print("[harvest] already complete (harvest.done) - keeping the existing identities.json", flush=True)
        else:
            run_harvest(a.input, a.private, a.jobs, deep=not a.no_deep_step)
    if a.cmd in ("transform", "all"):
        try:
            run_transform(a.input, a.output, a.private, a.jobs, link=not a.no_link,
                          rename_owners=not a.no_rename_owners, resume=a.resume, rename_paths=not a.no_rename_paths,
                          consume_input=a.consume_input, min_free_gib=a.min_free_gb, guard_every=a.guard_every)
        except diskguard.DiskLow:
            return 3
    if a.cmd in ("verify", "all"):
        rep = run_verify(a.input, a.output, a.private, a.jobs, expected_files=a.expected_files)
        print(json.dumps({k: v for k, v in rep.items() if k != "leak_scan"}, indent=1))
        print("leak_scan:", rep["leak_scan"])
        if not rep["ok"]:
            print("VERIFY FAILED", file=sys.stderr)
            verify_failed = True
    if a.cmd in ("readme", "all"):
        write_readme(a.private, a.out, a.attempt, a.notes)
        print("wrote", a.out)
    if a.cmd == "status":
        if not a.status_file:
            print("--status-file is required", file=sys.stderr)
            return 2
        d = status_mod.collect(a.stage, a.msg, a.private, work=os.path.dirname(os.path.abspath(a.private)),
                               guard_paths=[p for p in os.environ.get("DEID_GUARD_PATHS", "").split(":") if p.strip()],
                               log=a.log, input_dir=a.input if a.count_files else None,
                               output_dir=a.output if a.count_files else None)
        status_mod.write(a.status_file, d)
        if a.peak_file:
            status_mod.update_peak(a.peak_file, d)
        if a.summary_file:
            status_mod.write(a.summary_file, status_mod.summarize(a.private))
        return 0
    if a.cmd == "gate":
        attempt_dir = a.attempt_dir or os.path.dirname(os.path.abspath(a.output.rstrip("/\\")))
        ok, errors, notes = run_gate(a.private, a.output, attempt_dir, a.expected_files)
        for n_ in notes:
            print("note:", n_)
        for e_ in errors:
            print("GATE ERROR:", e_, file=sys.stderr)
        print("GATE PASSED - safe to upload" if ok else "GATE FAILED - do not upload", file=sys.stderr if not ok else sys.stdout)
        return 0 if ok else 1
    return 1 if verify_failed else 0


if __name__ == "__main__":
    sys.exit(main())
