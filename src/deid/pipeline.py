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


def run_transform(input_dir, output_dir, private, jobs, link=True, rename_owners=True, resume=False, rename_paths=True):
    os.makedirs(output_dir, exist_ok=True)
    os.makedirs(private, exist_ok=True)
    rels = walk_inputs(input_dir)
    done = set()
    map_path = os.path.join(private, "path_map.tsv")
    if resume and os.path.exists(map_path):
        with open(map_path, encoding="utf-8") as f:
            for line in f:
                done.add(line.split("\t", 1)[0])
    todo = [r for r in rels if r not in done]
    print(f"[transform] {len(rels)} inputs, {len(done)} already done, {len(todo)} to do", flush=True)
    stats = collections.Counter()
    actions = collections.Counter()
    t0 = time.time()
    mode = "a" if resume else "w"               # a fresh run must never inherit rows from an earlier run
    with open(map_path, mode, encoding="utf-8") as mapf, open(os.path.join(private, "defects.tsv"), mode, encoding="utf-8") as deff, \
            mp.Pool(jobs, initializer=_t_init, initargs=(input_dir, output_dir, private, link, rename_owners, rename_paths)) as pool:
        for i, (rel, rel_out, action, size, defects, delta) in enumerate(pool.imap_unordered(_t_one, todo, chunksize=8), 1):
            mapf.write(f"{rel}\t{rel_out}\t{action}\t{size}\n")
            for where, why in defects:
                deff.write(f"{where}\t{why}\n")
            actions[action] += 1
            stats.update(delta)
            if i % 2000 == 0:
                mapf.flush(); deff.flush()
                print(f"[transform] {i}/{len(todo)} {dict(actions)} {time.time()-t0:.0f}s", flush=True)
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


def run_verify(input_dir, output_dir, private, jobs) -> dict:
    rep: dict = {"checks": {}}
    ins = set(walk_inputs(input_dir))
    outs = set(walk_inputs(output_dir))
    rows = []
    with open(os.path.join(private, "path_map.tsv"), encoding="utf-8") as f:
        for line in f:
            rel, rel_out, action, size = line.rstrip("\n").split("\t")
            rows.append((rel, rel_out, action, int(size)))
    mapped_in = {r[0] for r in rows}
    out_names = [r[1] for r in rows]
    rep["inputs"], rep["outputs"], rep["mapped"] = len(ins), len(outs), len(mapped_in)
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
    # leak scan
    leaks_total, unreadable, leak_files = 0, 0, []
    tasks = [(r[1], os.path.join(output_dir, r[1])) for r in rows]
    with mp.Pool(jobs, initializer=_v_init, initargs=(private,)) as pool:
        for rel_out, leaks, status in pool.imap_unordered(_v_one, tasks, chunksize=32):
            if leaks:
                leaks_total += leaks
                leak_files.append(rel_out)
            if status != "ok":
                unreadable += 1
    rep["leak_scan"] = {"phrase_hits_in_output": leaks_total, "files_with_hits": len(leak_files),
                        "sample_files": leak_files[:20], "unreadable_views": unreadable}
    rep["checks"]["no_harvested_identity_in_output"] = leaks_total == 0
    rep["ok"] = all(rep["checks"].values())
    with open(os.path.join(private, "verify_report.json"), "w") as f:
        json.dump(rep, f, indent=1)
    return rep


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
    for name in ("harvest", "transform", "verify", "readme", "all"):
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
    a = ap.parse_args(argv)
    if a.cmd in ("harvest", "all"):
        run_harvest(a.input, a.private, a.jobs, deep=not a.no_deep_step)
    if a.cmd in ("transform", "all"):
        run_transform(a.input, a.output, a.private, a.jobs, link=not a.no_link,
                      rename_owners=not a.no_rename_owners, resume=a.resume, rename_paths=not a.no_rename_paths)
    if a.cmd in ("verify", "all"):
        rep = run_verify(a.input, a.output, a.private, a.jobs)
        print(json.dumps({k: v for k, v in rep.items() if k != "leak_scan"}, indent=1))
        print("leak_scan:", rep["leak_scan"])
        if not rep["ok"]:
            print("VERIFY FAILED", file=sys.stderr)
            if a.cmd == "verify":
                return 1
    if a.cmd in ("readme", "all"):
        write_readme(a.private, a.out, a.attempt, a.notes)
        print("wrote", a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
