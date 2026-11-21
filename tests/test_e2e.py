"""End-to-end: synthetic mini corpus -> harvest -> transform -> verify -> README."""
import json
import os

import pytest

import fixtures as fx
from deid import pipeline as P


def build_input(root):
    def w(rel, data):
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)
    O = fx.OWNER
    base = f"sources/github/{O}__arm"
    step = fx.step_bytes()
    w(f"{base}/cad/bracket.step", step)
    w(f"{base}/cad/bracket copy.step", step)                                   # exact duplicate
    w(f"{base}/cad/placeholder.step", fx.step_bytes(author="user", org="Company", with_entities=False, name="p.step").replace(fx.EMAIL.encode(), b"a@example.com"))
    w(f"{base}/drawings/bracket.pdf", fx.pdf_bytes())
    w(f"{base}/native/bracket.SLDPRT", fx.sldprt_bytes())
    w(f"{base}/native/asm.SLDASM", fx.sldprt_bytes())
    w(f"{base}/models/part.igs", fx.iges_bytes())
    w(f"{base}/models/part.sat", fx.sat_bytes())
    w(f"{base}/models/part.stl", fx.stl_ascii())
    w(f"{base}/docs/notes.svg", fx.svg_bytes())
    w(f"{base}/empty.step", b"")
    w(f"{base}/broken.pdf", b"%PDF-1.4\nnot a pdf " + fx.EMAIL.encode())
    w(f"{base}/part.tar.gz", fx.tar_gz_bytes({f"{fx.USER}/a.step": step}))
    w(f"{base}/img/logo.png", b"\x89PNG\r\n\x1a\n" + bytes(range(256)))
    w("sources/github/someone-else__proj/pack.zip", fx.zip_bytes({"nested/x.step": step, "n.zip": fx.zip_bytes({"y.step": step})}))
    w("sources/github/someone-else__proj/model.FCStd", fx.fcstd_bytes())
    w("sources/github/someone-else__proj/dl.7z.resume-partial", b"\x37\x7a\xbc\xaf partial")
    w(f"sources/catalog/Thing/ab12/pack.extracted/{fx.AUTHOR}/a.step", step)
    w("sources/web/example.org/paper.pdf", fx.pdf_bytes(author="user"))
    w(f"archives/{O}__arm__abc123.zip", fx.zip_bytes({f"{O}__arm/cad/bracket.step": step}))
    os.makedirs(os.path.join(root, ".get-input"), exist_ok=True)
    w(".get-input/manifest.tsv", b"ignored\n")


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    t = tmp_path_factory.mktemp("e2e")
    inp, out, priv = str(t / "input"), str(t / "out" / "corpus"), str(t / "private")
    build_input(inp)
    store = P.run_harvest(inp, priv, jobs=2)
    actions, stats = P.run_transform(inp, out, priv, jobs=2)
    rep = P.run_verify(inp, out, priv, jobs=2)
    P.write_readme(priv, str(t / "out" / "README.txt"), 1)
    return dict(inp=inp, out=out, priv=priv, store=store, actions=actions, stats=stats, rep=rep, t=t)


def test_walk_ignores_helper_dir(run):
    rels = P.walk_inputs(run["inp"])
    assert not any(r.startswith(".get-input") for r in rels) and len(rels) == 20


def test_coverage_and_verify_report(run):
    rep = run["rep"]
    assert rep["ok"], json.dumps(rep, indent=1)
    assert rep["inputs"] == rep["outputs"] == rep["mapped"] == 20
    assert rep["leak_scan"]["phrase_hits_in_output"] == 0


def test_identities_harvested(run):
    ph = run["store"].phrases
    assert fx.AUTHOR.lower() in ph and fx.ORG.lower() in ph
    assert fx.OWNER in run["store"].owners and "someone-else" in run["store"].owners
    assert "user" not in ph and "company" not in ph


def test_no_original_identity_anywhere_in_output_tree(run):
    bad = []
    for root, _d, files in os.walk(run["out"]):
        for name in files:
            full = os.path.join(root, name)
            rel = os.path.relpath(full, run["out"])
            for sec in ("quillfeather", "marlowe", "jonathan", "jmarlowe77"):
                if sec in rel.lower():
                    bad.append(("path", rel))
    assert not bad, bad


def test_extensions_and_layout_preserved(run):
    rows = [l.rstrip("\n").split("\t") for l in open(os.path.join(run["priv"], "path_map.tsv"), encoding="utf-8")]
    assert len(rows) == 20
    for rel, rel_out, action, _size in rows:
        assert rel.rsplit(".", 1)[-1] == rel_out.rsplit(".", 1)[-1]
        assert len(rel) == len(rel_out)
        a, b = rel.split("/"), rel_out.split("/")
        assert len(a) == len(b) and a[0] == b[0]                      # same depth, same top-level dir
        if a[0] == "sources":
            assert a[1] == b[1] or a[1] not in ("github", "gitlab", "catalog", "web")  # host dir kept


def test_extracted_folder_matches_archive_entry_naming(run):
    rows = {r[0]: r[1] for r in (l.rstrip("\n").split("\t") for l in open(os.path.join(run["priv"], "path_map.tsv"), encoding="utf-8"))}
    ext_out = rows[f"sources/catalog/Thing/ab12/pack.extracted/{fx.AUTHOR}/a.step"]
    folder = ext_out.split("/")[-2]
    assert fx.AUTHOR not in ext_out and len(folder) == len(fx.AUTHOR)


def test_duplicates_empty_and_defects_kept(run):
    outs = {os.path.relpath(os.path.join(r, f), run["out"]) for r, _d, fs in os.walk(run["out"]) for f in fs}
    assert sum(1 for o in outs if o.endswith(".step")) >= 5               # both duplicate copies kept
    sizes = {o: os.path.getsize(os.path.join(run["out"], o)) for o in outs}
    assert any(o.endswith("empty.step") and s == 0 for o, s in sizes.items())
    assert any(o.endswith("dl.7z.resume-partial") for o in outs)
    readme = open(run["t"] / "out" / "README.txt", encoding="utf-8").read()
    assert "broken.pdf" in readme and "pdf rewrite failed" in readme
    assert fx.EMAIL not in readme and fx.AUTHOR not in readme


def test_unchanged_files_are_hardlinks_and_changed_are_not(run):
    rows = [l.rstrip("\n").split("\t") for l in open(os.path.join(run["priv"], "path_map.tsv"), encoding="utf-8")]
    n_unchanged = 0
    for rel, rel_out, action, _s in rows:
        a, b = os.path.join(run["inp"], rel), os.path.join(run["out"], rel_out)
        if action == "unchanged":
            n_unchanged += 1
            assert os.stat(a).st_ino == os.stat(b).st_ino
        else:
            assert os.stat(a).st_ino != os.stat(b).st_ino
            assert open(a, "rb").read() != open(b, "rb").read()
    assert n_unchanged >= 5 and run["actions"]["rewritten"] >= 10


def test_inputs_never_modified(run):
    # step with author must still carry the original author in the INPUT tree
    p = os.path.join(run["inp"], f"sources/github/{fx.OWNER}__arm/cad/bracket.step")
    assert fx.AUTHOR.encode() in open(p, "rb").read()


def test_deterministic_rerun_gives_identical_output(run, tmp_path):
    out2 = str(tmp_path / "corpus2")
    # same salt + identities => same bytes (private dir reused read-only for the identity files)
    import shutil
    priv2 = str(tmp_path / "priv2")
    shutil.copytree(run["priv"], priv2)
    os.unlink(os.path.join(priv2, "path_map.tsv"))
    P.run_transform(run["inp"], out2, priv2, jobs=1)
    rows = [l.rstrip("\n").split("\t") for l in open(os.path.join(run["priv"], "path_map.tsv"), encoding="utf-8")]
    for rel, rel_out, *_ in rows:
        assert open(os.path.join(run["out"], rel_out), "rb").read() == open(os.path.join(out2, rel_out), "rb").read()


def _copy_state(run, tmp_path):
    """Private dir + output tree copied (real copies, no hard links) so a test can damage them freely."""
    import shutil
    priv, out = str(tmp_path / "pv"), str(tmp_path / "corpus")
    shutil.copytree(run["priv"], priv)
    shutil.copytree(run["out"], out)
    return priv, out


def _rows(priv):
    return [l.rstrip("\n").split("\t") for l in open(os.path.join(priv, "path_map.tsv"), encoding="utf-8")]


def test_resume_skips_done_when_outputs_exist(run, tmp_path):
    priv, out = _copy_state(run, tmp_path)
    actions, _ = P.run_transform(run["inp"], out, priv, jobs=1, resume=True)
    assert sum(actions.values()) == 0                                           # everything recorded AND present


def test_resume_redoes_rows_whose_output_is_missing(run, tmp_path):
    priv, out = _copy_state(run, tmp_path)
    rows = _rows(priv)
    gone = [r for r in rows if r[2] == "rewritten"][0], [r for r in rows if r[2] == "unchanged"][0]
    for r in gone:
        os.unlink(os.path.join(out, r[1]))
    resized = [r for r in rows if r[2] == "unchanged" and int(r[3]) > 0 and r not in gone][0]
    with open(os.path.join(out, resized[1]), "ab") as f:                        # recorded size no longer matches
        f.write(b"x")
        f.flush()
    actions, _ = P.run_transform(run["inp"], out, priv, jobs=1, resume=True)
    assert sum(actions.values()) == 3
    rows2 = _rows(priv)
    assert len(rows2) == 20 and len({r[0] for r in rows2}) == 20                # no duplicate rows left behind
    rep = P.run_verify(run["inp"], out, priv, jobs=1, expected_files=20)
    assert rep["ok"], json.dumps(rep, indent=1)
    for r in rows2:                                                             # redone files are byte-identical to a clean run
        assert open(os.path.join(out, r[1]), "rb").read() == open(os.path.join(run["out"], r[1]), "rb").read()


def test_resume_retries_error_rows_and_ignores_torn_lines(run, tmp_path):
    priv, out = _copy_state(run, tmp_path)
    rows = _rows(priv)
    rows[0][2] = "ERROR"
    with open(os.path.join(priv, "path_map.tsv"), "w", encoding="utf-8") as f:
        for r in rows:
            f.write("\t".join(r) + "\n")
        f.write("sources/torn\tline")                                          # crash while writing the last row
    actions, _ = P.run_transform(run["inp"], out, priv, jobs=1, resume=True)
    assert sum(actions.values()) == 1
    rows2 = _rows(priv)
    assert len(rows2) == 20 and all(r[2] != "ERROR" for r in rows2)


def test_real_part_files_not_clobbered_and_no_temp_left(tmp_path):
    inp, out, priv = str(tmp_path / "in"), str(tmp_path / "out"), str(tmp_path / "pv")
    d = os.path.join(inp, "sources", "github", "zzowner__r")
    os.makedirs(d)
    step = fx.step_bytes()
    open(os.path.join(d, "a.step"), "wb").write(step)
    open(os.path.join(d, "a.step.part"), "wb").write(b"genuine partial download")
    P.run_harvest(inp, priv, jobs=1)
    P.run_transform(inp, out, priv, jobs=1, rename_paths=False)
    outs = sorted(os.listdir(os.path.join(out, "sources", "github", "zzowner__r")))
    assert outs == ["a.step", "a.step.part"]
    assert open(os.path.join(out, "sources", "github", "zzowner__r", "a.step.part"), "rb").read() == b"genuine partial download"
    assert fx.AUTHOR.encode() not in open(os.path.join(out, "sources", "github", "zzowner__r", "a.step"), "rb").read()
    # stale temp from a crashed run is swept
    stale = os.path.join(out, "sources", "github", "zzowner__r", "b.step" + P.TMP_SUFFIX)
    open(stale, "wb").write(b"x")
    assert P.sweep_tmp(out) == 1 and not os.path.exists(stale)


def test_fresh_run_does_not_inherit_old_path_map(run, tmp_path):
    import shutil
    priv = str(tmp_path / "p")
    shutil.copytree(run["priv"], priv)
    P.run_transform(run["inp"], str(tmp_path / "o1"), priv, jobs=1)             # same private dir, no --resume
    rows = open(os.path.join(priv, "path_map.tsv"), encoding="utf-8").read().splitlines()
    assert len(rows) == 20 and len({r.split("\t")[0] for r in rows}) == 20


# --------------------------------------------------------------------------- verify strictness
def test_verify_report_counts_known_defects_and_has_no_unexplained_rewrites(run):
    ls = run["rep"]["leak_scan"]
    assert ls["unreadable_rewritten_unexplained"] == 0
    assert ls["unreadable_views"] == ls["unreadable_known_source_defects"] + ls["unreadable_unchanged_unexplained"]
    assert run["rep"]["generated_utc"] and os.path.realpath(run["out"]) == run["rep"]["output_dir"]


def test_verify_fails_when_a_rewritten_output_is_corrupt(run, tmp_path):
    priv, out = _copy_state(run, tmp_path)
    victim = [r for r in _rows(priv) if r[2] == "rewritten" and r[1].lower().endswith((".pdf", ".sldprt", ".zip"))][0]
    path = os.path.join(out, victim[1])
    os.unlink(path)                                                             # may be a hard link to nothing shared, but be safe
    with open(path, "wb") as f:
        f.write(b"%PDF-1.4\ngarbage that no parser accepts" if victim[1].lower().endswith(".pdf") else b"PK\x03\x04 truncated")
    rep = P.run_verify(run["inp"], out, priv, jobs=1, expected_files=20)
    assert rep["ok"] is False and rep["checks"]["rewritten_outputs_readable"] is False
    assert rep["leak_scan"]["unreadable_rewritten_unexplained"] == 1


def test_verify_expected_file_count(run, tmp_path):
    priv, out = _copy_state(run, tmp_path)
    assert P.run_verify(run["inp"], out, priv, jobs=1, expected_files=20)["ok"]
    rep = P.run_verify(run["inp"], out, priv, jobs=1, expected_files=21)
    assert rep["ok"] is False and rep["checks"]["input_count_is_expected"] is False


def test_verify_cli_returns_nonzero_on_failure(run, tmp_path):
    priv, out = _copy_state(run, tmp_path)
    os.unlink(os.path.join(out, _rows(priv)[3][1]))
    args = ["verify", "--input", run["inp"], "--output", out, "--private", priv, "--jobs", "1"]
    assert P.main(args) == 1
    # `all` re-runs harvest+transform (which restores the deleted file); a wrong expected count still must fail it
    assert P.main(["all", "--input", run["inp"], "--output", out, "--private", priv, "--jobs", "1",
                   "--expected-files", "21", "--out", str(tmp_path / "R.txt")]) == 1


# --------------------------------------------------------------------------- pre-upload gate
def _attempt(run, tmp_path, expected=20):
    import shutil
    att = tmp_path / "attempt-1"
    corpus, priv = str(att / "corpus"), str(tmp_path / "private")
    shutil.copytree(run["out"], corpus)
    shutil.copytree(run["priv"], priv)
    os.makedirs(att / "code" / "src" / "deid")
    (att / "code" / "src" / "deid" / "__init__.py").write_text("")
    (att / "README.txt").write_text("readme")
    rep = P.run_verify(run["inp"], corpus, priv, jobs=1, expected_files=expected)
    assert rep["ok"], json.dumps(rep, indent=1)
    return str(att), corpus, priv


def _gate(att, corpus, priv, expected=20):
    ok, errors, notes = P.run_gate(priv, corpus, att, expected)
    return ok, " | ".join(errors)


def test_gate_passes_a_complete_verified_attempt(run, tmp_path):
    att, corpus, priv = _attempt(run, tmp_path)
    ok, err = _gate(att, corpus, priv)
    assert ok, err
    assert P.main(["gate", "--output", corpus, "--private", priv, "--attempt-dir", att, "--expected-files", "20"]) == 0


def test_gate_refuses_without_or_with_failed_verify_report(run, tmp_path):
    att, corpus, priv = _attempt(run, tmp_path)
    rp = os.path.join(priv, "verify_report.json")
    rep = json.load(open(rp))
    rep["ok"] = False
    rep["checks"]["no_harvested_identity_in_output"] = False
    json.dump(rep, open(rp, "w"))
    ok, err = _gate(att, corpus, priv)
    assert not ok and "not ok" in err and "no_harvested_identity_in_output" in err
    os.unlink(rp)
    ok, err = _gate(att, corpus, priv)
    assert not ok and "verify_report.json is missing" in err
    assert P.main(["gate", "--output", corpus, "--private", priv, "--attempt-dir", att, "--expected-files", "20"]) == 1


def test_gate_refuses_wrong_file_count(run, tmp_path):
    att, corpus, priv = _attempt(run, tmp_path)
    ok, err = _gate(att, corpus, priv, expected=188722)
    assert not ok and "188722" in err
    os.unlink(os.path.join(corpus, _rows(priv)[0][1]))                         # a file vanishes after verify
    ok, err = _gate(att, corpus, priv)
    assert not ok and "corpus/ holds 19 files, expected 20" in err


def test_gate_refuses_stale_report_symlinks_temp_files_and_foreign_corpus(run, tmp_path):
    att, corpus, priv = _attempt(run, tmp_path)
    mp_ = os.path.join(priv, "path_map.tsv")
    future = os.path.getmtime(os.path.join(priv, "verify_report.json")) + 120
    os.utime(mp_, (future, future))
    ok, err = _gate(att, corpus, priv)
    assert not ok and "newer than verify_report.json" in err
    att2, corpus2, priv2 = _attempt(run, tmp_path / "b")
    open(os.path.join(corpus2, "x" + P.TMP_SUFFIX), "wb").write(b"t")
    os.symlink(os.path.join(corpus2, _rows(priv2)[0][1]), os.path.join(corpus2, "link.step"))
    ok, err = _gate(att2, corpus2, priv2)
    assert not ok and "leftover temp file" in err and "symlink in corpus" in err
    ok, err = _gate(att2, os.path.join(att, "corpus"), priv2)
    assert not ok and "different output directory" in err


def test_gate_refuses_bad_attempt_layout_and_private_material(run, tmp_path):
    att, corpus, priv = _attempt(run, tmp_path)
    (tmp_path / "attempt-1" / "notes.txt").write_text("stray")
    ok, err = _gate(att, corpus, priv)
    assert not ok and "unexpected entry" in err and "notes.txt" in err
    os.unlink(tmp_path / "attempt-1" / "notes.txt")
    open(os.path.join(att, "code", "path_map.tsv"), "w").write("secret")
    ok, err = _gate(att, corpus, priv)
    assert not ok and "private-looking file inside code/" in err
    os.unlink(os.path.join(att, "code", "path_map.tsv"))
    os.unlink(os.path.join(att, "README.txt"))
    ok, err = _gate(att, corpus, priv)
    assert not ok and "lacks README.txt" in err
    (tmp_path / "attempt-1" / "README.txt").write_text("readme")
    import shutil
    inside = os.path.join(att, "private")                                       # private material inside the upload tree
    shutil.copytree(priv, inside)
    ok, errors, _ = P.run_gate(inside, corpus, att, 20)
    assert not ok and any("private directory lives inside the attempt directory" in e for e in errors)
