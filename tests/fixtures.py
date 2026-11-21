"""Synthetic fixtures: STEP/IGES/SAT/STL/SVG/FCStd/PDF/OLE/zip/tar/gz with KNOWN fake identities.

All identities here are invented. A tiny OLE2 (CFB v3) writer is included so SolidWorks/Inventor-style property
sets can be tested without proprietary software, including the mini-stream (<4096 bytes) path.
"""
from __future__ import annotations

import gzip
import io
import struct
import tarfile
import zipfile

AUTHOR = "Jonathan Marlowe"
ORG = "Quillfeather Robotics Ltd"
EMAIL = "jonathan.marlowe@quillfeather-robotics.com"
USER = "jmarlowe77"
OWNER = "quillfeather-labs"
ALL_ORIGINALS = [AUTHOR, ORG, EMAIL, USER, OWNER, "Marlowe", "Quillfeather"]


# ----------------------------------------------------------------------------- STEP
def step_bytes(author=AUTHOR, org=ORG, with_entities=True, name=None, nbody=200) -> bytes:
    name = name or rf"C:\\Users\\{USER}\\Documents\\bracket.step"
    hdr = (
        "ISO-10303-21;\nHEADER;\n"
        "FILE_DESCRIPTION(('Bracket for the arm, contact " + EMAIL + "'),'2;1');\n"
        f"FILE_NAME('{name}','2020-01-02T03:04:05',('{author}'),('{org}'),'SolidWorks 2019','SolidWorks MBD','');\n"
        "FILE_SCHEMA(('AUTOMOTIVE_DESIGN { 1 0 10303 214 1 1 1 1 }'));\nENDSEC;\nDATA;\n"
    )
    body = []
    if with_entities:
        body += [
            f"#1=PERSON('P1','{AUTHOR.split()[1]}','{AUTHOR.split()[0]}',$,$,$);",
            f"#2=ORGANIZATION('O1','{ORG}','company');",
            f"#3=ADDRESS($,'12','Quill Lane',$,'Springfield','IL','62701','US',$,'555-0100','{EMAIL}',$);",
        ]
    for i in range(4, nbody):
        body.append(f"#{i}=CARTESIAN_POINT('',({i}.5,{i*2}.25,{i}.125));")
    return (hdr + "\n".join(body) + "\nENDSEC;\nEND-ISO-10303-21;\n").encode("latin-1")


# ----------------------------------------------------------------------------- IGES
def iges_bytes() -> bytes:
    def hol(s):
        return f"{len(s)}H{s}"
    fields = [",", ";", hol("PROD-1"), hol("bracket.igs"), hol("SolidWorks"), hol("v1"), "32", "38", "6", "99", "15",
              hol("PROD-1"), "1.0", "2", hol("MM"), "1", "0.1", hol("20200102.030405"), "1E-06", "100.0",
              hol(AUTHOR), hol(ORG), "11", "0", hol("20200102.030405")]
    g = ",".join(fields[:])
    # first two fields use the default delimiters
    g = ",,;" if False else g
    g = "1H,,1H;," + ",".join(fields[2:]) + ";"
    gl = [g[i:i + 72] for i in range(0, len(g), 72)]
    lines = ["Start section for " + EMAIL.ljust(72 - len("Start section for "))[:72] + "S      1"]
    for n, chunk in enumerate(gl, 1):
        lines.append(chunk.ljust(72) + f"G{n:7d}")
    lines.append(("     110       1       0       0       0       0       0       0".ljust(72)) + "D      1")
    lines.append("T      1G%7dD      1P      0" % len(gl) + " " * 31 + "T      1")
    return ("\n".join(lines) + "\n").encode("latin-1")


# ----------------------------------------------------------------------------- SAT / STL / SVG / FCStd
def sat_bytes() -> bytes:
    return (f"700 0 1 0\n@7 ACIS 7 @10 {AUTHOR.split()[0]}Soft @24 Mon Jan 02 03:04:05 2020\n1000 1e-06 1e-10\n"
            "-0 body $-1 -1 $-1 $1 $-1 $-1 #\n").encode()


def stl_ascii() -> bytes:
    return f"solid bracket by {EMAIL}\nfacet normal 0 0 1\nouter loop\nvertex 0 0 0\nvertex 1 0 0\nvertex 0 1 0\nendloop\nendfacet\nendsolid\n".encode()


def stl_binary() -> bytes:
    hdr = f"made by {EMAIL}".encode().ljust(80, b"\x00")
    return hdr + struct.pack("<I", 1) + struct.pack("<12fH", *([0.0] * 12), 0)


def svg_bytes() -> bytes:
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"><title>Drawing</title>'
            f'<text x="1" y="1">Designed by {AUTHOR} ({ORG}) {EMAIL}</text></svg>').encode()


def fcstd_bytes() -> bytes:
    doc = (f'<?xml version="1.0"?><Document SchemaVersion="4"><Properties Count="2">'
           f'<Property name="CreatedBy" type="App::PropertyString"><String value="{AUTHOR} &lt;{EMAIL}&gt;"/></Property>'
           f'<Property name="Company" type="App::PropertyString"><String value="{ORG}"/></Property></Properties></Document>')
    bio = io.BytesIO()
    with zipfile.ZipFile(bio, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("Document.xml", doc)
        z.writestr("GuiDocument.xml", "<Gui/>")
        z.writestr("brep.brp", "BREP-DATA" * 50)
    return bio.getvalue()


# ----------------------------------------------------------------------------- PDF
def pdf_bytes(author=AUTHOR) -> bytes:
    import pikepdf
    pdf = pikepdf.new()
    pdf.add_blank_page(page_size=(200, 200))
    pdf.docinfo["/Author"] = author
    pdf.docinfo["/Title"] = f"Drawing by {ORG}"
    pdf.docinfo["/Producer"] = "SolidWorks PDF"
    with pdf.open_metadata(set_pikepdf_as_editor=False, update_docinfo=False) as m:
        m["dc:creator"] = [author]
        m["dc:title"] = f"Drawing by {ORG}"
    out = io.BytesIO()
    pdf.save(out)
    return out.getvalue()


# ----------------------------------------------------------------------------- OLE2 (CFB v3)
_END, _FATSECT, _FREE = 0xFFFFFFFE, 0xFFFFFFFD, 0xFFFFFFFF


def _propset(fmtid_le: bytes, props: list[tuple[int, object]], codepage=1252) -> bytes:
    """props: [(pid, str|int)]; strings become VT_LPSTR; codepage property written first."""
    entries = [(1, ("i2", codepage))] + [(pid, v) for pid, v in props]
    blobs = []
    for pid, v in entries:
        if isinstance(v, tuple):
            blobs.append(struct.pack("<HHhH", 2, 0, v[1], 0))
        else:
            raw = v.encode("cp1252") + b"\x00"
            blobs.append(struct.pack("<HHI", 0x1E, 0, len(raw)) + raw + b"\x00" * (-len(raw) % 4))
    n = len(entries)
    offs, cur = [], 8 + 8 * n
    for b in blobs:
        offs.append(cur)
        cur += len(b)
    body = struct.pack("<II", cur, n) + b"".join(struct.pack("<II", pid, o) for (pid, _), o in zip(entries, offs)) + b"".join(blobs)
    hdr = struct.pack("<HHI", 0xFFFE, 0, 0x00020006) + b"\x00" * 16 + struct.pack("<I", 1) + fmtid_le + struct.pack("<I", 48)
    return hdr + body


def summary_stream(author, lastauthor, title="Bracket") -> bytes:
    import uuid
    return _propset(uuid.UUID("F29F85E0-4FF9-1068-AB91-08002B27B3D9").bytes_le,
                    [(2, title), (4, author), (6, f"Contact {EMAIL}"), (8, lastauthor), (18, "SolidWorks")])


def docsummary_stream(company, manager) -> bytes:
    import uuid
    return _propset(uuid.UUID("D5CDD502-2E9C-101B-9397-08002B2CF9AE").bytes_le, [(14, manager), (15, company)])


def build_cfb(streams: dict[str, bytes]) -> bytes:
    """Minimal valid CFB v3 writer (512-byte sectors, mini stream for <4096-byte streams)."""
    SS = 512
    small = {k: v for k, v in streams.items() if len(v) < 4096}
    large = {k: v for k, v in streams.items() if len(v) >= 4096}
    # mini stream
    mini_data, mini_fat, mini_start = b"", [], {}
    for k, v in small.items():
        mini_start[k] = len(mini_fat)
        nsec = max(1, -(-len(v) // 64))
        for i in range(nsec):
            mini_fat.append(len(mini_fat) + 1 if i < nsec - 1 else _END)
        mini_data += v.ljust(nsec * 64, b"\x00")
    names = list(streams)
    n_entries = 1 + len(names)
    dir_sectors = -(-n_entries * 128 // SS)
    body = dir_sectors + (1 if small else 0) + (-(-len(mini_data) // SS) if small else 0) + \
        sum(-(-len(v) // SS) for v in large.values())
    n_fat = 1
    while n_fat * 128 < body + n_fat:
        n_fat += 1
    assert n_fat <= 109, "fixture writer supports up to ~7 MB (header DIFAT only)"
    sector_cursor = n_fat                               # sectors 0..n_fat-1 = FAT
    dir_first = sector_cursor
    sector_cursor += dir_sectors
    minifat_sec = None
    if small:
        minifat_sec = sector_cursor
        sector_cursor += 1
    mini_cont_first = None
    mini_cont_n = -(-len(mini_data) // SS) if small else 0
    if small:
        mini_cont_first = sector_cursor
        sector_cursor += mini_cont_n
    large_first = {}
    for k, v in large.items():
        large_first[k] = sector_cursor
        sector_cursor += -(-len(v) // SS)
    total = sector_cursor
    fat = [_FREE] * (128 * n_fat)
    for i in range(n_fat):
        fat[i] = _FATSECT

    def chain(first, n):
        for i in range(n):
            fat[first + i] = first + i + 1 if i < n - 1 else _END
    chain(dir_first, dir_sectors)
    if small:
        fat[minifat_sec] = _END
        chain(mini_cont_first, mini_cont_n)
    for k, v in large.items():
        chain(large_first[k], -(-len(v) // SS))

    def entry(name, etype, left, right, child, start, size):
        nb = (name + "\x00").encode("utf-16-le")
        return (nb.ljust(64, b"\x00") + struct.pack("<HBB", len(nb), etype, 1) + struct.pack("<III", left, right, child) +
                b"\x00" * 16 + struct.pack("<I", 0) + b"\x00" * 16 + struct.pack("<IQ", start, size))
    order = sorted(range(len(names)), key=lambda i: (len(names[i]), names[i].upper()))
    nxt = {}
    for a, b in zip(order, order[1:]):
        nxt[a] = b
    ents = [entry("Root Entry", 5, _FREE, _FREE, (1 + order[0]) if names else _FREE,
                  mini_cont_first if small else _END, len(mini_data))]
    for i, k in enumerate(names):
        right = (1 + nxt[i]) if i in nxt else _FREE
        if k in small:
            ents.append(entry(k, 2, _FREE, right, _FREE, mini_start[k], len(streams[k])))
        else:
            ents.append(entry(k, 2, _FREE, right, _FREE, large_first[k], len(streams[k])))
    dir_blob = b"".join(ents).ljust(dir_sectors * SS, b"\x00")
    difat = list(range(n_fat)) + [_FREE] * (109 - n_fat)
    header = (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 16 + struct.pack("<HHHHH", 0x3E, 3, 0xFFFE, 9, 6) + b"\x00" * 6 +
              struct.pack("<IIIIIIIII", 0, n_fat, dir_first, 0, 4096, minifat_sec if small else _END, 1 if small else 0, _END, 0) +
              struct.pack("<109I", *difat))
    assert len(header) == 512
    out = bytearray(header)
    out += struct.pack(f"<{128 * n_fat}I", *fat)
    out += dir_blob
    if small:
        mf = (mini_fat + [_FREE] * 128)[:128]
        out += struct.pack("<128I", *mf)
        out += mini_data.ljust(mini_cont_n * SS, b"\x00")
    for k, v in large.items():
        out += v.ljust(-(-len(v) // SS) * SS, b"\x00")
    return bytes(out)


def sldprt_bytes(author=AUTHOR, company=ORG) -> bytes:
    return build_cfb({
        "\x05SummaryInformation": summary_stream(author, author),
        "\x05DocumentSummaryInformation": docsummary_stream(company, "Manager Person"),
        "Contents": bytes(range(256)) * 24,
    })


# ----------------------------------------------------------------------------- archives
def zip_bytes(members: dict[str, bytes]) -> bytes:
    bio = io.BytesIO()
    with zipfile.ZipFile(bio, "w", zipfile.ZIP_DEFLATED) as z:
        for n, b in members.items():
            z.writestr(n, b)
    return bio.getvalue()


def tar_gz_bytes(members: dict[str, bytes]) -> bytes:
    bio = io.BytesIO()
    with tarfile.open(fileobj=bio, mode="w:gz") as t:
        for n, b in members.items():
            ti = tarfile.TarInfo(n)
            ti.size = len(b)
            ti.uname = ti.gname = USER
            ti.uid = ti.gid = 1000
            t.addfile(ti, io.BytesIO(b))
    return bio.getvalue()


def gz_bytes(inner: bytes, name: str) -> bytes:
    bio = io.BytesIO()
    with gzip.GzipFile(filename=name, mode="wb", fileobj=bio) as g:
        g.write(inner)
    return bio.getvalue()
