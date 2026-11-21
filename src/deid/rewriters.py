"""Format-aware, length-preserving rewriters.

Contract: every rewriter returns data with the SAME LENGTH as it received, except container formats
(ZIP/TAR/GZIP) whose members are renamed/rewritten and re-packed. Anything that cannot be handled raises
or returns the input untouched; the caller records a defect and keeps the original bytes (never drops a file).
"""
from __future__ import annotations

import gzip
import io
import mmap
import os
import re
import shutil
import struct
import tarfile
import tempfile
import uuid
import zipfile
from dataclasses import dataclass, field

from .identity import EMAIL_RE, Replacer
from .names import Pseudonymizer, identity_candidate, is_placeholder

OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
TMP_SUFFIX = ".deid-tmp-9f3a"   # unique temp suffix; real corpus files may end in .part
MEM_LIMIT = 300 * 1024 * 1024          # in-memory rewriters (OLE, PDF, ZIP, TAR, GZIP, text)
TEXT_FULL_LIMIT = 32 * 1024 * 1024      # whole-file replace for text up to this size
TEXT_HEAD_ZONE = 1024 * 1024            # larger text files: only head zone is rewritten
MAX_DEPTH = 4

STEP_EXTS = {"step", "stp", "p21", "stpz_"}
IGES_EXTS = {"igs", "iges"}
ZIP_EXTS = {"zip", "fcstd", "3mf", "f3d", "docx", "xlsx", "pptx", "odt", "ods", "odp", "jar", "kmz", "amf",
            "dwfx", "xps", "epub", "ipynb_", "sketch", "skp_", "lbrn2", "mtz", "whl", "nupkg"}
TEXT_EXTS = {"svg", "dxf", "obj", "wrl", "vrml", "ps", "eps", "txt", "md", "rst", "json", "xml", "csv", "tsv",
             "html", "htm", "yaml", "yml", "ini", "cfg", "log", "cmake", "py", "c", "cpp", "h", "hpp", "js",
             "ts", "css", "scad", "gcode", "nc", "ngc", "dae", "x3d", "amf", "kicad_pcb", "kicad_sch",
             "kicad_pro", "sch", "brd", "fcmacro", "readme", "bat", "sh", "tex", "bib", "gpx", "nsvg", "x_t",
             "xmt_txt", "dwf_", "ngsolve", "geo", "msh", "inp", "unv", "neu", "pro", "mac", "lisp", "lsp",
             "scr", "ply", "off", "mtl", "pov", "inc", "ldr", "mpd", "dat", "xmp", "rdf", "plist", "toml"}


@dataclass
class Ctx:
    rep: Replacer
    pz: Pseudonymizer
    rename_owners: bool = True
    defects: list = field(default_factory=list)
    stats: dict = field(default_factory=dict)

    def bump(self, k: str, n: int = 1) -> None:
        self.stats[k] = self.stats.get(k, 0) + n

    def defect(self, where: str, why: str) -> None:
        self.defects.append((where, why))

    def map_entry(self, name: str) -> str:
        return self.rep.map_path(name, rename_owners=False)


# =====================================================================================
# STEP (ISO 10303-21)
# =====================================================================================
_STR_LIT = re.compile(r"'((?:[^']|'')*)'")
_ESC = re.compile(r"\\(?:X2\\[0-9A-Fa-f]*\\X0\\|X4\\[0-9A-Fa-f]*\\X0\\|X\\[0-9A-Fa-f]{2}|S\\.|P[A-I]\\)")


def _map_keep_escapes(content: str, fn) -> str:
    out, last = [], 0
    for m in _ESC.finditer(content):
        out.append(fn(content[last:m.start()]))
        out.append(m.group(0))
        last = m.end()
    out.append(fn(content[last:]))
    r = "".join(out)
    assert len(r) == len(content)
    return r


def _split_top_level(body: str) -> list[tuple[int, int]]:
    items, depth, start, i, n, in_str = [], 0, 0, 0, len(body), False
    while i < n:
        c = body[i]
        if in_str:
            if c == "'":
                if i + 1 < n and body[i + 1] == "'":
                    i += 2
                    continue
                in_str = False
        elif c == "'":
            in_str = True
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        elif c == "," and depth == 0:
            items.append((start, i))
            start = i + 1
        i += 1
    items.append((start, n))
    return items


def _find_close(s: str, open_idx: int) -> int:
    """index of the ')' matching an already-consumed '(' (string aware)."""
    depth, i, n, in_str = 1, open_idx, len(s), False
    while i < n:
        c = s[i]
        if in_str:
            if c == "'":
                if i + 1 < n and s[i + 1] == "'":
                    i += 2
                    continue
                in_str = False
        elif c == "'":
            in_str = True
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def map_person_like(s: str, ctx: Ctx, kind: str = "person") -> str:
    """Map a person/org string. Placeholders and vendor-only strings are left alone. E-mails keep shape."""
    if not identity_candidate(s):
        return ctx.rep.replace(s)
    out, last = [], 0
    for m in EMAIL_RE.finditer(s):
        out.append(_map_words(s[last:m.start()], ctx, kind))
        out.append(ctx.pz.email(m.group(0)))
        last = m.end()
    out.append(_map_words(s[last:], ctx, kind))
    r = "".join(out)
    assert len(r) == len(s)
    return r


def _map_words(s: str, ctx: Ctx, kind: str) -> str:
    return ctx.pz.org(s) if kind == "org" else ctx.pz.map_text(s, keep_words=True, digits=True)


def _map_literals(seg: str, fn) -> str:
    return _STR_LIT.sub(lambda m: "'" + _map_keep_escapes(m.group(1), fn) + "'", seg)


def rewrite_step_header(hdr: str, ctx: Ctx) -> str:
    rep = ctx.rep
    out = hdr
    # FILE_NAME(name, time_stamp, author, organization, preprocessor_version, originating_system, authorization)
    m = re.search(r"FILE_NAME\s*\(", out, re.I)
    if m:
        close = _find_close(out, m.end())
        if close > 0:
            body = out[m.end():close]
            items = _split_top_level(body)
            roles = {0: rep.replace, 2: lambda s: map_person_like(s, ctx, "person"),
                     3: lambda s: map_person_like(s, ctx, "org"), 4: rep.replace, 5: rep.replace,
                     6: lambda s: map_person_like(s, ctx, "person")}
            new = []
            for idx, (a, b) in enumerate(items):
                fn = roles.get(idx)
                new.append(_map_literals(body[a:b], fn) if fn else body[a:b])
            out = out[:m.end()] + ",".join(new) + out[close:]
    m = re.search(r"FILE_DESCRIPTION\s*\(", out, re.I)
    if m:
        close = _find_close(out, m.end())
        if close > 0:
            out = out[:m.end()] + _map_literals(out[m.end():close], rep.replace) + out[close:]
    # any other header entity (SCHEMA_POPULATION, etc.): patterns only
    assert len(out) == len(hdr)
    return out


_ENT = re.compile(
    rb"(#\d+\s*=\s*)(PERSON|ORGANIZATION|ADDRESS|PERSONAL_ADDRESS|ORGANIZATIONAL_ADDRESS)(\s*\()(.*?)(\)\s*;)",
    re.S)
_ENT_TRIGGERS = (b"PERSON(", b"ORGANIZATION(", b"ADDRESS(", b"PERSON (", b"ORGANIZATION (", b"ADDRESS (")


def _rewrite_entities(block: bytes, ctx: Ctx) -> bytes:
    def sub(m: re.Match) -> bytes:
        name = m.group(2).decode("ascii")
        args = m.group(4).decode("latin-1")
        items = _split_top_level(args)
        new = []
        for idx, (a, b) in enumerate(items):
            seg = args[a:b]
            if name == "PERSON":
                fn = None if idx == 0 else (lambda s: map_person_like(s, ctx, "person"))
            elif name == "ORGANIZATION":
                fn = (lambda s: map_person_like(s, ctx, "org")) if idx == 1 else (None if idx == 0 else ctx.rep.replace)
            else:  # ADDRESS family: every string is address data except short country codes
                fn = lambda s: s if (len(s) <= 3 and s.isalpha()) else _map_words(s, ctx, "person")
            new.append(_map_literals(seg, fn) if fn else seg)
        return m.group(1) + m.group(2) + m.group(3) + ",".join(new).encode("latin-1") + m.group(5)
    return _ENT.sub(sub, block)


def step_needs_scan(path: str, size: int) -> bool:
    if size == 0:
        return False
    with open(path, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
        return any(mm.find(t) != -1 for t in _ENT_TRIGGERS)


def rewrite_step_stream(src_path: str, dst_path: str, ctx: Ctx) -> bool:
    """Stream-rewrite a STEP file. Returns True when anything changed (dst written), False otherwise
    (dst NOT created)."""
    size = os.path.getsize(src_path)
    with open(src_path, "rb") as fin:
        head = fin.read(min(size, 1 << 20))
        end = head.find(b"ENDSEC;")
        if not head.lstrip().startswith(b"ISO-10303-21") or end < 0:
            raise ValueError("no STEP header found")
        hdr_end = end + len(b"ENDSEC;")
        hdr = head[:hdr_end].decode("latin-1")
        new_hdr = rewrite_step_header(hdr, ctx)
        scan = step_needs_scan(src_path, size)
        if new_hdr == hdr and not scan:
            return False
        changed = new_hdr != hdr
        tmp = dst_path + TMP_SUFFIX
        with open(tmp, "wb") as fout:
            fout.write(new_hdr.encode("latin-1"))
            carry = head[hdr_end:]
            while True:
                chunk = fin.read(8 << 20)
                buf = carry + chunk
                if not chunk:
                    block, carry = buf, b""
                else:
                    cut = buf.rfind(b";")
                    if cut < 0 or len(buf) > (256 << 20):
                        block, carry = (buf, b"") if len(buf) > (256 << 20) else (b"", buf)
                    else:
                        block, carry = buf[:cut + 1], buf[cut + 1:]
                if block:
                    if any(t in block for t in _ENT_TRIGGERS):
                        nb = _rewrite_entities(block, ctx)
                        changed = changed or nb != block
                        block = nb
                    fout.write(block)
                if not chunk:
                    break
        if not changed:
            os.unlink(tmp)
            return False
        os.replace(tmp, dst_path)
        return True


def rewrite_step_bytes(data: bytes, ctx: Ctx) -> bytes:
    end = data.find(b"ENDSEC;", 0, 1 << 20)
    if end < 0 or not data.lstrip().startswith(b"ISO-10303-21"):
        return data
    hdr_end = end + 7
    new_hdr = rewrite_step_header(data[:hdr_end].decode("latin-1"), ctx).encode("latin-1")
    body = data[hdr_end:]
    if any(t in body for t in _ENT_TRIGGERS):
        body = _rewrite_entities(body, ctx)
    return new_hdr + body


# =====================================================================================
# IGES  (80-column records, Hollerith strings)
# =====================================================================================
def _iges_fields(g: str):
    """yield (start, end, is_hollerith_string) for each Global-section field."""
    n, i = len(g), 0
    pd, rd = ",", ";"
    k = 0
    fields = []
    while i < n and k < 30:
        if k == 0:
            if g[i] == ",":           # defaulted parameter delimiter
                fields.append((i, i, False)); i += 1; k += 1; continue
            m = re.match(r"(\d+)H(.)", g[i:])
            if not m:
                break
            pd = m.group(2)
            fields.append((i + m.end() - 1, i + m.end(), True)); i += m.end() + 1; k += 1; continue
        if k == 1:
            if g[i] == pd:
                fields.append((i, i, False)); i += 1; k += 1; continue
            m = re.match(r"(\d+)H(.)", g[i:])
            if not m:
                break
            rd = m.group(2)
            fields.append((i + m.end() - 1, i + m.end(), True)); i += m.end() + 1; k += 1; continue
        m = re.match(r"\s*(\d+)H", g[i:])
        if m:
            ln = int(m.group(1))
            s = i + m.end()
            fields.append((s, s + ln, True))
            i = s + ln
        else:
            j = i
            while j < n and g[j] not in (pd, rd):
                j += 1
            fields.append((i, j, False))
            i = j
        if i < n and g[i] == rd:
            break
        if i < n and g[i] == pd:
            i += 1
        k += 1
    return fields


def rewrite_iges(data: bytes, ctx: Ctx) -> bytes:
    text = data.decode("latin-1")
    nl = "\r\n" if "\r\n" in text[:200] else "\n"
    lines = text.split(nl)
    gidx = [i for i, l in enumerate(lines) if len(l) >= 73 and l[72] == "G"]
    sidx = [i for i, l in enumerate(lines) if len(l) >= 73 and l[72] == "S"]
    changed = False
    for i in sidx:                                            # Start section = free text
        new = ctx.rep.replace(lines[i][:72]) + lines[i][72:]
        changed = changed or new != lines[i]
        lines[i] = new
    if gidx:
        g = "".join(lines[i][:72] for i in gidx)
        out = list(g)
        for k, (a, b, is_str) in enumerate(_iges_fields(g)):
            if not is_str or b <= a:
                continue
            val = g[a:b]
            if k in (2, 3):                                   # product id, file name
                nv = ctx.rep.replace(val)
            elif k == 20:                                     # author
                nv = map_person_like(val, ctx, "person")
            elif k == 21:                                     # author organisation
                nv = map_person_like(val, ctx, "org")
            elif k in (4, 5):
                nv = ctx.rep.replace(val)
            else:
                continue
            out[a:b] = list(nv)
        ng = "".join(out)
        if ng != g:
            changed = True
            for j, i in enumerate(gidx):
                lines[i] = ng[j * 72:(j + 1) * 72] + lines[i][72:]
    return nl.join(lines).encode("latin-1") if changed else data


# =====================================================================================
# Generic text, SAT, STL
# =====================================================================================
_XML_ATTR = re.compile(r'(?i)\b(CreatedBy|LastModifiedBy|Author|Creator|Designer|Company|Manager|Owner)\s*=\s*"([^"]*)"')
_3MF_META = re.compile(r'(?is)(<metadata\s+name="(?:Designer|Copyright|Author|Creator|Company)"[^>]*>)([^<]*)(</metadata>)')
_FCSTD_PROP = re.compile(r'(?is)(<Property\s+name="(?:CreatedBy|LastModifiedBy|Company|Author|Maker|Designer)"[^>]*>\s*<String\s+value=")([^"]*)(")')


def _xml_identity(s: str, ctx: Ctx) -> str:
    def attr(m):
        kind = "org" if m.group(1).lower() in ("company",) else "person"
        return m.group(0)[:m.start(2) - m.start(0)] + map_person_like(m.group(2), ctx, kind) + '"'
    s = _XML_ATTR.sub(attr, s)
    s = _3MF_META.sub(lambda m: m.group(1) + map_person_like(m.group(2), ctx, "person") + m.group(3), s)
    s = _FCSTD_PROP.sub(lambda m: m.group(1) + map_person_like(m.group(2), ctx, "person") + m.group(3), s)
    return s


def rewrite_text_bytes(data: bytes, ctx: Ctx, name: str = "") -> bytes:
    s = data.decode("latin-1")
    out = ctx.rep.replace(s)
    if "<" in s and (name.lower().endswith((".xml", ".svg", ".model", ".rels", ".fcstd", ".rdf", ".xmp")) or
                     name == "" or "CreatedBy" in s or "metadata" in s):
        out = _xml_identity(out, ctx)
    return out.encode("latin-1") if out != s else data


def atomic_write(dst: str, data: bytes) -> None:
    """Write via dst.part then rename, so a crash never leaves a truncated file under the final name."""
    tmp = dst + TMP_SUFFIX
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, dst)


def rewrite_text_file(src: str, dst: str, ctx: Ctx, size: int) -> bool:
    if size <= TEXT_FULL_LIMIT:
        with open(src, "rb") as f:
            data = f.read()
        new = rewrite_text_bytes(data, ctx, src)
        if new == data:
            return False
        atomic_write(dst, new)
        return True
    with open(src, "rb") as fin:
        head = fin.read(TEXT_HEAD_ZONE)
        new = rewrite_text_bytes(head, ctx, src)
        if new == head:
            return False
        with open(dst + TMP_SUFFIX, "wb") as fout:
            fout.write(new)
            shutil.copyfileobj(fin, fout, 8 << 20)
    os.replace(dst + TMP_SUFFIX, dst)
    return True


def rewrite_sat(data: bytes, ctx: Ctx) -> bytes:
    """ACIS SAT: header lines only (version / product id / tolerances); entity records untouched."""
    nl = data.find(b"\n", 0, 4096)
    if nl < 0:
        return data
    cut = 0
    for _ in range(3):
        j = data.find(b"\n", cut, cut + 4096)
        if j < 0:
            break
        cut = j + 1
    head = data[:cut]
    new = ctx.rep.replace_bytes(head)
    return new + data[cut:] if new != head else data


def rewrite_stl(data: bytes, ctx: Ctx) -> bytes:
    if data[:5].lower() == b"solid" and b"facet" in data[:2048]:   # ASCII STL
        nl = data.find(b"\n")
        head = data[:nl] if nl > 0 else data[:200]
        new = ctx.rep.replace_bytes(head)
        return new + data[len(head):] if new != head else data
    head = data[:80]                                               # binary STL 80-byte header
    new = ctx.rep.replace_bytes(head)
    return new + data[80:] if new != head else data


# =====================================================================================
# OLE2 compound files  (SolidWorks, Inventor, legacy Office, ...) - property sets only
# =====================================================================================
FMTID_SUMMARY = uuid.UUID("F29F85E0-4FF9-1068-AB91-08002B27B3D9")
FMTID_DOCSUM = uuid.UUID("D5CDD502-2E9C-101B-9397-08002B2CF9AE")
_PERSON_PIDS = {FMTID_SUMMARY: {4, 8}, FMTID_DOCSUM: {14}}
_ORG_PIDS = {FMTID_DOCSUM: {15}}
_KEEP_PIDS = {FMTID_SUMMARY: {18}}   # application name


def rewrite_propset(data: bytes, ctx: Ctx) -> bytes:
    """MS-OLEPS property set stream; string values rewritten in place, stream length unchanged."""
    if len(data) < 48 or data[:2] != b"\xfe\xff":
        return data
    buf = bytearray(data)
    nsets = struct.unpack_from("<I", buf, 24)[0]
    if nsets == 0 or nsets > 8:
        return data
    for si in range(nsets):
        try:
            fm = uuid.UUID(bytes_le=bytes(buf[28 + 20 * si:44 + 20 * si]))
            base = struct.unpack_from("<I", buf, 44 + 20 * si)[0]
            nprops = struct.unpack_from("<I", buf, base + 4)[0]
            if nprops > 4096:
                continue
            props = [struct.unpack_from("<II", buf, base + 8 + 8 * i) for i in range(nprops)]
            codepage = 1252
            for pid, off in props:
                if pid == 1 and struct.unpack_from("<H", buf, base + off)[0] == 2:
                    codepage = struct.unpack_from("<H", buf, base + off + 4)[0]
            for pid, off in props:
                vt = struct.unpack_from("<H", buf, base + off)[0]
                if vt not in (0x1E, 0x1F) or pid == 0:
                    continue
                ln = struct.unpack_from("<I", buf, base + off + 4)[0]
                start = base + off + 8
                if vt == 0x1E:
                    raw = bytes(buf[start:start + ln])
                    enc = "utf-8" if codepage == 65001 else ("latin-1" if codepage in (0, 1252, 28591) else f"cp{codepage}")
                    try:
                        txt = raw.split(b"\x00", 1)[0].decode(enc)
                    except (UnicodeDecodeError, LookupError):
                        enc = "latin-1"; txt = raw.split(b"\x00", 1)[0].decode("latin-1")
                    new = _prop_policy(fm, pid, txt, ctx)
                    if new != txt:
                        nb = new.encode(enc, "replace")
                        if len(nb) <= ln:
                            buf[start:start + ln] = nb + b"\x00" * (ln - len(nb))
                else:
                    raw = bytes(buf[start:start + 2 * ln])
                    txt = raw.decode("utf-16-le", "replace").split("\x00", 1)[0]
                    new = _prop_policy(fm, pid, txt, ctx)
                    if new != txt:
                        nb = new.encode("utf-16-le")
                        if len(nb) <= 2 * ln:
                            buf[start:start + 2 * ln] = nb + b"\x00" * (2 * ln - len(nb))
        except (struct.error, ValueError):
            continue
    return bytes(buf)


def _prop_policy(fmtid: uuid.UUID, pid: int, text: str, ctx: Ctx) -> str:
    if pid in _KEEP_PIDS.get(fmtid, ()):
        return text
    if pid in _PERSON_PIDS.get(fmtid, ()):
        return map_person_like(text, ctx, "person")
    if pid in _ORG_PIDS.get(fmtid, ()):
        return map_person_like(text, ctx, "org")
    return ctx.rep.replace(text)


def rewrite_ole(data: bytes, ctx: Ctx) -> bytes:
    import olefile
    bio = io.BytesIO(data)
    ole = olefile.OleFileIO(bio, write_mode=True)
    changed = False
    try:
        for entry in ole.listdir(streams=True, storages=False):
            if not entry[-1].startswith("\x05"):
                continue
            raw = ole.openstream(entry).read()
            new = rewrite_propset(raw, ctx)
            if new != raw and len(new) == len(raw):
                ole.write_stream(entry, new)
                changed = True
    finally:
        ole.close()
    return bio.getvalue() if changed else data


# =====================================================================================
# PDF
# =====================================================================================
_XMP_LI = re.compile(rb"(<(?:dc:creator|dc:contributor|pdf:Author|xmp:Author|dc:publisher)[^>]*>(?:\s*<rdf:(?:Seq|Bag|Alt)>)?\s*(?:<rdf:li[^>]*>)?)([^<]+)")


def rewrite_pdf(data: bytes, ctx: Ctx) -> bytes:
    import pikepdf
    pdf = pikepdf.open(io.BytesIO(data))
    changed = False
    try:
        di = pdf.docinfo
        for key in list(di.keys()):
            try:
                val = str(di[key])
            except Exception:
                continue
            if key == "/Author":
                new = map_person_like(val, ctx, "person")
            elif key in ("/Title", "/Subject", "/Keywords", "/Creator", "/Producer", "/Company", "/Manager"):
                new = ctx.rep.replace(val)
            else:
                continue
            if new != val:
                di[key] = pikepdf.String(new)
                changed = True
        md = pdf.Root.get("/Metadata")
        if md is not None:
            raw = bytes(md.read_bytes())
            s = ctx.rep.replace_bytes(raw)
            s = _XMP_LI.sub(lambda m: m.group(1) + map_person_like(m.group(2).decode("utf-8", "replace"), ctx).encode("utf-8")
                            if len(map_person_like(m.group(2).decode("utf-8", "replace"), ctx).encode("utf-8")) == len(m.group(2))
                            else m.group(0), s)
            if s != raw:
                md.write(s)
                changed = True
        if not changed:
            return data
        out = io.BytesIO()
        pdf.save(out, linearize=False)
        return out.getvalue()
    finally:
        pdf.close()


# =====================================================================================
# Containers
# =====================================================================================
def rewrite_zip(data: bytes, ctx: Ctx, depth: int, label: str) -> bytes:
    """Rewrite member names and contents. If nothing changed at all the ORIGINAL bytes are returned, so the file
    stays byte-identical (and is hard-linked, not re-deflated and copied)."""
    zin = zipfile.ZipFile(io.BytesIO(data))
    out = io.BytesIO()
    touched = False
    with zipfile.ZipFile(out, "w") as zout:
        for info in zin.infolist():
            newname = ctx.map_entry(info.filename)
            touched |= newname != info.filename
            zi = zipfile.ZipInfo(newname, date_time=info.date_time)
            zi.compress_type = info.compress_type if info.compress_type in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED) else zipfile.ZIP_DEFLATED
            zi.external_attr = info.external_attr
            zi.create_system = info.create_system
            if info.is_dir():
                zout.writestr(zi, b"")
                continue
            if info.flag_bits & 0x1:
                raise ValueError("encrypted member")
            if info.file_size > MEM_LIMIT:
                ctx.defect(f"{label}!{newname}", "archive member too large to rewrite; copied unchanged")
                zout.writestr(zi, zin.read(info))
                continue
            body = zin.read(info)
            new = process_blob(info.filename, body, ctx, depth + 1, label=f"{label}!{newname}")
            touched |= new != body
            zout.writestr(zi, new)
        comment = ctx.rep.replace_bytes(zin.comment)
        touched |= comment != zin.comment
        zout.comment = comment
    return out.getvalue() if touched else data


def rewrite_tar(data: bytes, ctx: Ctx, depth: int, label: str) -> bytes:
    """As rewrite_zip: untouched archives are returned byte-for-byte."""
    tin = tarfile.open(fileobj=io.BytesIO(data), mode="r:*")
    out = io.BytesIO()
    touched = False
    with tarfile.open(fileobj=out, mode="w", format=tarfile.PAX_FORMAT) as tout:
        for m in tin:
            ni = tarfile.TarInfo(ctx.map_entry(m.name))
            touched |= ni.name != m.name or bool(m.uid or m.gid or m.uname or m.gname)
            ni.mode, ni.mtime, ni.type = m.mode, m.mtime, m.type
            ni.uid = ni.gid = 0
            ni.uname = ni.gname = ""
            if m.issym() or m.islnk():
                ni.linkname = ctx.map_entry(m.linkname)
                touched |= ni.linkname != m.linkname
            if m.isfile():
                body = tin.extractfile(m).read()
                new = process_blob(m.name, body, ctx, depth + 1, label=f"{label}!{ni.name}")
                touched |= new != body
                ni.size = len(new)
                tout.addfile(ni, io.BytesIO(new))
            else:
                tout.addfile(ni)
    return out.getvalue() if touched else data


def rewrite_gzip(data: bytes, name: str, ctx: Ctx, depth: int, label: str) -> bytes:
    raw = gzip.decompress(data)
    inner = name[:-3] if name.lower().endswith(".gz") else (name[:-4] + ".tar" if name.lower().endswith(".tgz") else name)
    new = process_blob(inner, raw, ctx, depth + 1, label=label)
    if new == raw:
        return data
    buf = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buf, mtime=0) as g:
        g.write(new)
    return buf.getvalue()


# =====================================================================================
# Dispatch
# =====================================================================================
def _ext(name: str) -> str:
    base = name.rsplit("/", 1)[-1]
    return base.rsplit(".", 1)[-1].lower() if "." in base else ""


def looks_text(head: bytes) -> bool:
    if not head or b"\x00" in head:
        return False
    printable = sum(1 for b in head if 9 <= b <= 13 or 32 <= b < 127 or b >= 128)
    return printable / len(head) > 0.97


def classify(name: str, head: bytes) -> str:
    ext = _ext(name)
    h = head.lstrip()[:20]
    if ext in STEP_EXTS or h.startswith(b"ISO-10303-21"):
        return "step"
    if ext in IGES_EXTS:
        return "iges"
    if head.startswith(OLE_MAGIC):
        return "ole"
    if head[:1024].find(b"%PDF-") != -1 and ext in ("pdf", "ai", ""):
        return "pdf"
    if head.startswith((b"PK\x03\x04", b"PK\x05\x06")) and ext in ZIP_EXTS:
        return "zip"
    if head[:2] == b"\x1f\x8b" and ext in ("gz", "tgz"):
        return "gzip"
    if ext == "tar" or head[257:262] == b"ustar":
        return "tar"
    if ext == "sat":
        return "sat"
    if ext == "stl":
        return "stl"
    if ext in TEXT_EXTS or (ext not in ("png", "jpg", "jpeg", "gif", "bmp", "zip", "7z", "rar", "z") and looks_text(head[:4096])):
        return "text"
    return "binary"


def process_blob(name: str, data: bytes, ctx: Ctx, depth: int = 0, label: str = "") -> bytes:
    """bytes -> bytes (used for archive members). Raises on failure of a top-level call; inside archives a
    member that cannot be processed is kept as is and recorded."""
    kind = classify(name, data[:4096])
    try:
        if kind == "step":
            return rewrite_step_bytes(data, ctx)
        if kind == "iges":
            return rewrite_iges(data, ctx)
        if kind == "ole":
            return rewrite_ole(data, ctx)
        if kind == "pdf":
            return rewrite_pdf(data, ctx)
        if kind == "sat":
            return rewrite_sat(data, ctx)
        if kind == "stl":
            return rewrite_stl(data, ctx)
        if kind == "text":
            return rewrite_text_bytes(data, ctx, name)
        if depth >= MAX_DEPTH and kind in ("zip", "tar", "gzip"):
            ctx.defect(label or name, "archive nesting deeper than limit; copied unchanged")
            return data
        if kind == "zip":
            return rewrite_zip(data, ctx, depth, label or name)
        if kind == "tar":
            return rewrite_tar(data, ctx, depth, label or name)
        if kind == "gzip":
            return rewrite_gzip(data, name, ctx, depth, label or name)
    except Exception as e:  # keep original bytes; never drop a difficult file
        ctx.defect(label or name, f"{kind} rewrite failed ({type(e).__name__}); copied unchanged")
        return data
    return data


def process_file(src: str, dst: str, name_for_kind: str, ctx: Ctx, link: bool = True) -> str:
    """Write dst from src. Returns 'unchanged' | 'rewritten' | 'defect'. dst's directory must exist."""
    size = os.path.getsize(src)
    with open(src, "rb") as f:
        head = f.read(4096)
    kind = classify(name_for_kind, head)
    changed = False
    try:
        if kind == "step":
            changed = rewrite_step_stream(src, dst, ctx)
        elif kind == "text":
            changed = rewrite_text_file(src, dst, ctx, size)
        elif kind in ("iges", "ole", "pdf", "sat", "stl", "zip", "tar", "gzip"):
            if size > MEM_LIMIT:
                ctx.defect(name_for_kind, f"{kind} file larger than {MEM_LIMIT >> 20} MB; copied unchanged")
            else:
                with open(src, "rb") as f:
                    data = f.read()
                new = process_blob(name_for_kind, data, ctx, 0, label=name_for_kind)
                changed = new != data
                if changed:
                    atomic_write(dst, new)
    except Exception as e:
        ctx.defect(name_for_kind, f"{kind} rewrite failed ({type(e).__name__}: {str(e)[:80]}); copied unchanged")
        for leftover in (dst, dst + TMP_SUFFIX):
            if os.path.lexists(leftover):
                os.unlink(leftover)
        changed = False
    if changed:
        ctx.bump("rewritten_" + kind)
        return "rewritten"
    ctx.bump("unchanged_" + kind)
    if os.path.lexists(dst):
        os.unlink(dst)
    if link:
        try:
            os.link(src, dst)
            return "unchanged"
        except OSError:
            pass
    shutil.copyfile(src, dst + TMP_SUFFIX)
    os.replace(dst + TMP_SUFFIX, dst)
    return "unchanged"
