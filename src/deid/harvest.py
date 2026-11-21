"""Phase 1: harvest identity strings (read-only) from identity-bearing metadata fields.

Only fields that are *defined* to carry identities are read (STEP FILE_NAME author/organization and
PERSON/ORGANIZATION entities, IGES global author/organization, OLE summary properties, PDF Author,
FCStd / 3MF creator attributes) plus GitHub/GitLab owner names taken from the corpus folder layout.
"""
from __future__ import annotations

import io
import mmap
import os
import re
import struct
import uuid
import zipfile

from .names import identity_candidate
from .rewriters import (FMTID_DOCSUM, FMTID_SUMMARY, IGES_EXTS, OLE_MAGIC, STEP_EXTS, _ENT, _XML_ATTR, _3MF_META,
                        _FCSTD_PROP, _STR_LIT, _find_close, _iges_fields, _split_top_level, classify)

Found = list  # list[tuple[str kind, str value]]


def _lits(seg: str) -> list[str]:
    return [m.group(1).replace("''", "'") for m in _STR_LIT.finditer(seg)]


def harvest_step_header(hdr: str) -> Found:
    out: Found = []
    m = re.search(r"FILE_NAME\s*\(", hdr, re.I)
    if m:
        close = _find_close(hdr, m.end())
        if close > 0:
            body = hdr[m.end():close]
            items = _split_top_level(body)
            for idx, (a, b) in enumerate(items):
                kind = {2: "person", 3: "org", 6: "person"}.get(idx)
                if kind:
                    out += [(kind, s) for s in _lits(body[a:b])]
    return out


def harvest_step(path: str, deep: bool = True) -> Found:
    out: Found = []
    with open(path, "rb") as f:
        head = f.read(1 << 20)
    end = head.find(b"ENDSEC;")
    if end > 0:
        out += harvest_step_header(head[:end + 7].decode("latin-1"))
    if deep and os.path.getsize(path) > 0:
        with open(path, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
            for trig, kind in ((b"PERSON(", "person"), (b"ORGANIZATION(", "org")):
                pos = 0
                while True:
                    pos = mm.find(trig, pos)
                    if pos < 0:
                        break
                    st = mm.rfind(b"#", max(0, pos - 24), pos)
                    en = mm.find(b";", pos, pos + 2000)
                    pos += len(trig)
                    if st < 0 or en < 0:
                        continue
                    m = _ENT.match(mm[st:en + 1])
                    if not m:
                        continue
                    args = m.group(4).decode("latin-1")
                    items = _split_top_level(args)
                    lits = [(_lits(args[a:b]) or [""])[0] for (a, b) in items]
                    if m.group(2) == b"PERSON" and len(lits) >= 3:
                        first, last = lits[2] if len(lits) > 2 else "", lits[1]
                        full = " ".join(x for x in (first, last) if x and x != "$")
                        out.append(("person", full))
                    elif m.group(2) == b"ORGANIZATION" and len(lits) >= 2:
                        out.append(("org", lits[1]))
    return out


def harvest_iges(data: bytes) -> Found:
    text = data.decode("latin-1")
    lines = text.splitlines()
    g = "".join(l[:72] for l in lines if len(l) >= 73 and l[72] == "G")
    out: Found = []
    for k, (a, b, is_str) in enumerate(_iges_fields(g)):
        if is_str and b > a and k == 20:
            out.append(("person", g[a:b]))
        if is_str and b > a and k == 21:
            out.append(("org", g[a:b]))
    return out


def _propset_strings(data: bytes):
    """yield (fmtid, pid, text) for every string property of an OLE property-set stream."""
    if len(data) < 48 or data[:2] != b"\xfe\xff":
        return
    nsets = struct.unpack_from("<I", data, 24)[0]
    if not 0 < nsets <= 8:
        return
    for si in range(nsets):
        try:
            fm = uuid.UUID(bytes_le=bytes(data[28 + 20 * si:44 + 20 * si]))
            base = struct.unpack_from("<I", data, 44 + 20 * si)[0]
            n = struct.unpack_from("<I", data, base + 4)[0]
            if n > 4096:
                continue
            for i in range(n):
                pid, off = struct.unpack_from("<II", data, base + 8 + 8 * i)
                vt = struct.unpack_from("<H", data, base + off)[0]
                ln = struct.unpack_from("<I", data, base + off + 4)[0]
                s = base + off + 8
                if vt == 0x1E:
                    yield fm, pid, data[s:s + ln].split(b"\x00", 1)[0].decode("latin-1")
                elif vt == 0x1F:
                    yield fm, pid, data[s:s + 2 * ln].decode("utf-16-le", "replace").split("\x00", 1)[0]
        except (struct.error, ValueError):
            continue


def harvest_ole(data: bytes) -> Found:
    import olefile
    out: Found = []
    ole = olefile.OleFileIO(io.BytesIO(data))
    try:
        for entry in ole.listdir(streams=True, storages=False):
            if not entry[-1].startswith("\x05"):
                continue
            for fm, pid, text in _propset_strings(ole.openstream(entry).read()):
                if (fm == FMTID_SUMMARY and pid in (4, 8)) or (fm == FMTID_DOCSUM and pid == 14):
                    out.append(("person", text))
                elif fm == FMTID_DOCSUM and pid == 15:
                    out.append(("org", text))
    finally:
        ole.close()
    return out


def harvest_pdf(data: bytes) -> Found:
    import pikepdf
    out: Found = []
    with pikepdf.open(io.BytesIO(data)) as pdf:
        for key, kind in (("/Author", "person"), ("/Company", "org"), ("/Manager", "person")):
            if key in pdf.docinfo:
                out.append((kind, str(pdf.docinfo[key])))
    return out


def harvest_zip_xml(data: bytes) -> Found:
    out: Found = []
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for info in z.infolist():
            n = info.filename.lower()
            if info.file_size < (4 << 20) and (n.endswith("document.xml") or n.endswith(".model")):
                s = z.read(info).decode("utf-8", "replace")
                for m in _XML_ATTR.finditer(s):
                    out.append(("org" if m.group(1).lower() == "company" else "person", m.group(2)))
                for m in _3MF_META.finditer(s):
                    out.append(("person", m.group(2)))
                for m in _FCSTD_PROP.finditer(s):
                    out.append(("person", m.group(2)))
    return out


_OWNER_DIR = re.compile(r"^([^_/]+?)__")


def harvest_path(rel: str) -> Found:
    parts = rel.split("/")
    if len(parts) >= 3 and parts[0] == "sources" and parts[1] in ("github", "gitlab"):
        m = _OWNER_DIR.match(parts[2])
        if m:
            return [("owner", m.group(1))]
    if len(parts) >= 2 and parts[0] == "archives":
        m = _OWNER_DIR.match(parts[1])
        if m:
            return [("owner", m.group(1))]
    return []


def harvest_file(path: str, rel: str, deep_step: bool = True) -> Found:
    """Never raises; returns [] on any parse problem."""
    ext = rel.rsplit(".", 1)[-1].lower() if "." in rel.rsplit("/", 1)[-1] else ""
    try:
        size = os.path.getsize(path)
        if size == 0:
            return []
        with open(path, "rb") as f:
            head = f.read(4096)
        kind = classify(rel, head)
        if kind == "step":
            found = harvest_step(path, deep_step)
        elif size > 300 * 1024 * 1024:
            return []
        else:
            with open(path, "rb") as f:
                data = f.read()
            if kind == "iges":
                found = harvest_iges(data)
            elif kind == "ole":
                found = harvest_ole(data)
            elif kind == "pdf":
                found = harvest_pdf(data)
            elif kind == "zip" and ext in ("fcstd", "3mf", "zip"):
                found = harvest_zip_xml(data) if ext != "zip" else []
            else:
                return []
        return [(k, v) for k, v in found if v and identity_candidate(v)]
    except Exception:
        return []
