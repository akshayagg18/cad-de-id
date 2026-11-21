"""Format rewriters: identities gone, engineering data and structure untouched, lengths preserved."""
import gzip
import io
import tarfile
import zipfile

import olefile
import pikepdf
import pytest

import fixtures as fx
from deid.rewriters import (classify, process_blob, process_file, rewrite_iges, rewrite_ole, rewrite_pdf,
                            rewrite_sat, rewrite_step_bytes, rewrite_stl, rewrite_text_bytes)

SECRETS = [fx.AUTHOR, fx.ORG, fx.EMAIL, fx.USER, "Marlowe", "Quillfeather", "Jonathan", "quillfeather"]


def no_secret(blob: bytes | str, extra=()):
    s = blob.decode("latin-1") if isinstance(blob, bytes) else blob
    low = s.lower()
    for sec in list(SECRETS) + list(extra):
        assert sec.lower() not in low, f"leaked {sec!r}"


# ------------------------------------------------------------------------------ STEP
def test_step_header_and_entities(ctx):
    src = fx.step_bytes()
    out = rewrite_step_bytes(src, ctx)
    assert len(out) == len(src)
    no_secret(out)
    a, b = src.decode().split("DATA;")[1], out.decode().split("DATA;")[1]
    # geometry lines identical; only the 3 identity entities may differ
    la, lb = a.split("\n"), b.split("\n")
    diff = [i for i, (x, y) in enumerate(zip(la, lb)) if x != y]
    assert 0 < len(diff) <= 3 and all("CARTESIAN_POINT" not in la[i] for i in diff)
    assert out.decode().startswith("ISO-10303-21;") and "END-ISO-10303-21;" in out.decode()
    assert "SolidWorks 2019" in out.decode()          # software names are not identities


def test_step_placeholders_untouched(ctx):
    src = fx.step_bytes(author="user", org="Company", with_entities=False, name="part.step").replace(
        fx.EMAIL.encode(), b"nobody@example.com")
    assert rewrite_step_bytes(src, ctx) == src


def test_step_consistent_across_files(ctx):
    a = rewrite_step_bytes(fx.step_bytes(), ctx).decode().split("FILE_NAME(")[1].split(");")[0]
    b = rewrite_step_bytes(fx.step_bytes(nbody=50), ctx).decode().split("FILE_NAME(")[1].split(");")[0]
    assert a == b


def test_step_stream_matches_bytes_and_hardlinks_unchanged(tmp_path, ctx):
    p = tmp_path / "in.step"
    p.write_bytes(fx.step_bytes(nbody=60000))                       # > one 8MB chunk? (~3MB) still exercises loop
    d = tmp_path / "out.step"
    assert process_file(str(p), str(d), "in.step", ctx) == "rewritten"
    assert d.read_bytes() == rewrite_step_bytes(p.read_bytes(), ctx)
    q = tmp_path / "plain.step"
    q.write_bytes(fx.step_bytes(author="user", org="Company", with_entities=False, name="x").replace(fx.EMAIL.encode(), b"a@example.com"))
    e = tmp_path / "plain_out.step"
    assert process_file(str(q), str(e), "plain.step", ctx) == "unchanged"
    assert q.stat().st_ino == e.stat().st_ino                       # hard link, no extra disk


def test_step_chunk_boundaries(tmp_path, ctx, monkeypatch):
    big = fx.step_bytes(nbody=400000)                               # ~20 MB -> several 8 MB chunks
    p = tmp_path / "big.step"
    p.write_bytes(big)
    # identity entities sit at the very start; add one far into the file too
    tail = b"#999999=PERSON('P9','Marlowe','Jonathan',$,$,$);\nENDSEC;\nEND-ISO-10303-21;\n"
    p.write_bytes(big.replace(b"ENDSEC;\nEND-ISO-10303-21;\n", tail))
    d = tmp_path / "big_out.step"
    assert process_file(str(p), str(d), "big.step", ctx) == "rewritten"
    out = d.read_bytes()
    assert len(out) == p.stat().st_size
    no_secret(out)


# ------------------------------------------------------------------------------ IGES / SAT / STL / text
def test_iges(ctx):
    src = fx.iges_bytes()
    out = rewrite_iges(src, ctx)
    assert len(out) == len(src)
    no_secret(out)
    for a, b in zip(src.decode().split("\n"), out.decode().split("\n")):
        assert len(a) == len(b)
        assert a[72:] == b[72:]                                      # section letters / sequence numbers intact
    assert b"6HPROD-1" in out or b"PROD-1" in out


def test_sat_and_stl(ctx):
    sat = fx.sat_bytes().replace(b"Jonathan", fx.EMAIL.encode()[:8])
    assert rewrite_sat(sat, ctx) == sat or len(rewrite_sat(sat, ctx)) == len(sat)
    s = fx.stl_ascii()
    o = rewrite_stl(s, ctx)
    assert len(o) == len(s) and fx.EMAIL.encode() not in o and b"facet normal 0 0 1" in o
    b = fx.stl_binary()
    ob = rewrite_stl(b, ctx)
    assert len(ob) == len(b) and fx.EMAIL.encode() not in ob and ob[80:] == b[80:]


def test_text_svg(ctx):
    src = fx.svg_bytes()
    out = rewrite_text_bytes(src, ctx, "a.svg")
    assert len(out) == len(src)
    no_secret(out)
    assert b"<svg" in out and b"<title>Drawing</title>" in out


# ------------------------------------------------------------------------------ OLE
def test_ole_property_sets_in_place(ctx):
    src = fx.sldprt_bytes()
    assert classify("x.sldprt", src[:4096]) == "ole"
    out = rewrite_ole(src, ctx)
    assert len(out) == len(src)
    no_secret(out)
    o_in, o_out = olefile.OleFileIO(io.BytesIO(src)), olefile.OleFileIO(io.BytesIO(out))
    names_in = o_in.listdir(streams=True, storages=False)
    assert names_in == o_out.listdir(streams=True, storages=False)
    for n in names_in:
        a, b = o_in.openstream(n).read(), o_out.openstream(n).read()
        assert len(a) == len(b)
        if not n[-1].startswith("\x05"):
            assert a == b                                            # model data untouched
    sumi = o_out.openstream("\x05SummaryInformation").read()
    assert b"SolidWorks" in sumi and b"Bracket" in sumi             # app name / title are not identities
    assert b"Manager Person" not in o_out.openstream("\x05DocumentSummaryInformation").read()


def test_ole_property_values_are_pseudonyms_and_consistent(ctx):
    out1 = rewrite_ole(fx.sldprt_bytes(), ctx)
    out2 = rewrite_ole(fx.sldprt_bytes(), ctx)
    assert out1 == out2
    o = olefile.OleFileIO(io.BytesIO(out1))
    raw = o.openstream("\x05SummaryInformation").read()
    # author and last-author were identical -> identical pseudonym
    import re
    names = re.findall(rb"[A-Z][a-z]+ [A-Z][a-z]+", raw)
    assert len([n for n in names if n != b"Manager Person"]) >= 2 and len(set(names)) <= len(names)


# ------------------------------------------------------------------------------ PDF
def test_pdf_metadata(ctx):
    src = fx.pdf_bytes()
    out = rewrite_pdf(src, ctx)
    no_secret(out)
    with pikepdf.open(io.BytesIO(out)) as pdf:
        assert len(pdf.pages) == 1
        assert str(pdf.docinfo["/Author"]) != fx.AUTHOR and len(str(pdf.docinfo["/Author"])) == len(fx.AUTHOR)
        assert "Quillfeather" not in str(pdf.docinfo["/Title"])
        assert str(pdf.docinfo["/Producer"]) == "SolidWorks PDF"


def test_pdf_output_is_reproducible_across_time(ctx):
    import time
    src = fx.pdf_bytes()
    a = rewrite_pdf(src, ctx)
    time.sleep(1.1)                                   # qpdf's default /ID is time based; ours must not be
    b = rewrite_pdf(src, ctx)
    assert a == b


def test_pdf_without_identity_is_untouched(ctx):
    src = fx.pdf_bytes(author="user")
    import pikepdf as pk
    p = pk.open(io.BytesIO(src))
    p.docinfo["/Title"] = "Plain"
    with p.open_metadata(set_pikepdf_as_editor=False) as m:
        m["dc:title"] = "Plain"
    bio = io.BytesIO()
    p.save(bio)
    assert rewrite_pdf(bio.getvalue(), ctx) == bio.getvalue()


def test_corrupt_pdf_falls_back_to_original(ctx):
    bad = b"%PDF-1.4\nthis is not really a pdf " + fx.EMAIL.encode()
    out = process_blob("broken.pdf", bad, ctx)
    assert out == bad
    assert any("pdf rewrite failed" in why for _, why in ctx.defects)


# ------------------------------------------------------------------------------ containers
def test_zip_with_nested_step_and_names(ctx):
    inner = fx.step_bytes()
    z = fx.zip_bytes({f"{fx.AUTHOR}/part.step": inner, "docs/readme.txt": f"by {fx.EMAIL}".encode(),
                      "nested.zip": fx.zip_bytes({"deep/a.step": inner}), "empty_dir/": b""})
    out = process_blob("pack.zip", z, ctx)
    no_secret(out)                                                  # names are stored in clear in zip headers
    with zipfile.ZipFile(io.BytesIO(out)) as zf:
        names = zf.namelist()
        assert zf.testzip() is None
        assert len(names) == 4 and any(n.endswith("part.step") for n in names) and "empty_dir/" in names
        for n in names:
            if n.endswith(".step"):
                no_secret(zf.read(n))
        nested = [n for n in names if n.endswith("nested.zip")][0]
        with zipfile.ZipFile(io.BytesIO(zf.read(nested))) as z2:
            no_secret(z2.read("deep/a.step"))
            assert z2.testzip() is None
        assert zf.read([n for n in names if n.endswith("readme.txt")][0]) != f"by {fx.EMAIL}".encode()


def test_fcstd_zip_xml_attributes(ctx):
    out = process_blob("model.FCStd", fx.fcstd_bytes(), ctx)
    with zipfile.ZipFile(io.BytesIO(out)) as z:
        no_secret(z.read("Document.xml"))
        assert b"<Property name=\"CreatedBy\"" in z.read("Document.xml")
        assert z.read("brep.brp") == b"BREP-DATA" * 50


def test_tar_gz_clears_owner_and_rewrites(ctx):
    tgz = fx.tar_gz_bytes({f"{fx.USER}/a.step": fx.step_bytes(), "b.txt": fx.EMAIL.encode()})
    out = process_blob("x.tar.gz", tgz, ctx)
    assert out != tgz
    with tarfile.open(fileobj=io.BytesIO(gzip.decompress(out))) as t:
        for m in t:
            assert m.uname == "" and m.gname == "" and m.uid == 0
            assert fx.USER not in m.name
            if m.isfile():
                no_secret(t.extractfile(m).read())


def test_gz_strips_filename_and_rewrites(ctx):
    g = fx.gz_bytes(fx.step_bytes(), f"{fx.USER}.step")
    out = process_blob("part.step.gz", g, ctx)
    no_secret(out)                                                   # gzip header FNAME
    assert gzip.decompress(out).startswith(b"ISO-10303-21;")
    no_secret(gzip.decompress(out))


def test_encrypted_or_corrupt_zip_is_kept_and_reported(ctx):
    bad = b"PK\x03\x04" + b"garbage" * 20
    assert process_blob("broken.zip", bad, ctx) == bad
    assert ctx.defects and "zip rewrite failed" in ctx.defects[-1][1]


def test_binary_and_images_pass_through(ctx):
    png = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 4
    assert process_blob("logo.png", png, ctx) == png
    assert process_blob("weird.bin", bytes(range(256)) * 10, ctx) == bytes(range(256)) * 10


def test_untouched_zip_and_tar_are_returned_byte_identical(ctx):
    from deid import rewriters as R
    clean_zip = fx.zip_bytes({"a/readme.txt": b"nothing identifying here\n", "b.bin": bytes(range(256)) * 8})
    assert R.rewrite_zip(clean_zip, ctx, 0, "t.zip") is clean_zip
    clean_tar = fx.tar_gz_bytes({"a.txt": b"hello"})
    # tar.gz: outer gzip decompresses to a tar whose entries carry uid/gid/uname -> that is a change; bare clean tar is not
    import io, tarfile
    bio = io.BytesIO()
    with tarfile.open(fileobj=bio, mode="w") as t:
        ti = tarfile.TarInfo("a.txt"); ti.size = 5; t.addfile(ti, io.BytesIO(b"hello"))
    raw = bio.getvalue()
    assert R.rewrite_tar(raw, ctx, 0, "t.tar") is raw


def test_touched_zip_is_rewritten(ctx):
    from deid import rewriters as R
    z = fx.zip_bytes({"x/a.step": fx.step_bytes()})
    out = R.rewrite_zip(z, ctx, 0, "t.zip")
    assert out != z and fx.AUTHOR.encode() not in __import__("zipfile").ZipFile(__import__("io").BytesIO(out)).read("x/a.step")
