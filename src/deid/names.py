"""Deterministic, keyed, LENGTH-PRESERVING pseudonym generation.

Every public mapping function returns a string with exactly the same number of
characters as its input. That single invariant lets the same pseudonyms be used
inside text CAD formats (STEP, IGES Hollerith counts, SAT counted strings),
fixed-size OLE property values, file names, ZIP entries and raw byte buffers
without having to move any offset.

Mapping is token based: every alphabetic run is mapped independently, so
"John Smith", "Smith, John" and "john.smith@x.com" stay mutually consistent.
"""
from __future__ import annotations

import hashlib
import hmac
import re

# --------------------------------------------------------------------------------------
# Small realistic pools (lower case). Pools are indexed by length; anything not covered
# is produced by the pronounceable generator below.
# --------------------------------------------------------------------------------------
_FIRST = """adam alan alex amy anna anton ari ben bruno carl carla chris clara dan dana david
diana eli elena emil emma eric erik eva felix fiona frank gabe gina greg hana hans helen henry
ian ida igor ines ivan jack jade jan jana jean joel john jose juan kai karl kate kira lars laura
leo lena liam lisa lucas luis lukas maja marc maria mark marta max maya mia mike nadia nick nina
noah nora olga omar oscar otto paul pete petra rafa rita rob rosa ruth sam sara sean sofia stan
tara theo tina tom uma vera vic will zoe""".split()
_LAST = """adler baker barnes bauer becker bell berg blake brandt brooks brown carter chen clark cole
cook cooper cruz dahl davis diaz dunn eaton evans fabre fischer flynn ford foster fox frank fuchs
gates gray green grant hahn hall hayes hart hoff hughes ito jones kane keller kim king klein koch
kraus lane lang lee lewis long lopez lund marsh meyer miller moore morris moss nash neal nolan
ortiz owens park perry pierce quinn ramos reed reyes rhodes rice rossi ruiz ryan sato scott shaw
silva smith stone tanaka taylor thomas todd vogel wade wagner walsh ward weber wells west wolf
wood young zimmer""".split()

POOL_BY_LEN: dict[int, list[str]] = {}
for _w in sorted(set(_FIRST + _LAST)):
    POOL_BY_LEN.setdefault(len(_w), []).append(_w)

_CONS = "bdfgklmnprstvz"
_VOW = "aeiou"

# Words that are never mapped inside person/organisation strings (legal forms, titles).
KEEP_WORDS = {
    "inc", "ltd", "llc", "gmbh", "co", "corp", "corporation", "company", "ag", "sa", "srl", "bv", "plc",
    "limited", "kg", "ug", "oy", "ab", "as", "nv", "spa", "pty", "pte", "lp", "llp", "the", "of", "and",
    "for", "de", "la", "van", "von", "der", "den", "mr", "mrs", "ms", "dr", "prof", "jr", "sr", "ii", "iii",
    "phd", "msc", "eng", "ing", "dipl", "gesellschaft", "group", "holding", "holdings", "international",
}

# Values that are placeholders, not identities.
PLACEHOLDERS = {
    "", "user", "users", "username", "company", "unknown", "none", "n/a", "na", "admin", "administrator",
    "author", "anonymous", "owner", "name", "your name", "yourname", "your company", "organization",
    "organisation", "org", "default", "guest", "test", "tbd", "-", "?", "noname", "no name", "not specified",
    "unspecified", "undefined", "null", "nil", "x", "xx", "xxx", "me", "myself", "someone", "person",
    "engineer", "designer", "student", "home", "pc", "computer", "windows", "root", "public", "shared",
    "generic", "sample", "example", "demo", "temp", "tmp", "private", "personal", "work", "office",
    "$", "''", '""', "(none)", "<none>", "[none]", "no author", "no company",
}

# Software / vendor words: strings made only of these are not a person's or firm's identity.
VENDOR_WORDS = {
    "autodesk", "solidworks", "dassault", "systemes", "siemens", "ptc", "parametric", "technology",
    "freecad", "inventor", "catia", "creo", "pro", "engineer", "onshape", "fusion", "sketchup", "blender",
    "rhino", "mcneel", "openscad", "opencascade", "occt", "cadquery", "spatial", "parasolid", "unigraphics",
    "microsoft", "google", "github", "gitlab", "nx", "plm", "software", "inc", "corporation", "corp",
    "ltd", "llc", "gmbh", "co", "the", "cad", "step", "iges", "acis", "kubotek", "kisters", "tech", "soft",
    "bentley", "systems", "hexagon", "ansys", "altair", "ugs", "sdrc", "ideas", "cimatron", "solid", "edge",
    "mastercam", "cadence", "mentor", "graphics", "autocad", "revit", "navisworks", "meshlab", "salome",
}

_ALPHA_RUN = re.compile(r"[^\W\d_]+", re.UNICODE)
_DIGIT_RUN = re.compile(r"\d+")


def _digest(salt: bytes, key: str, counter: int = 0) -> bytes:
    return hmac.new(salt, f"{counter}\x00{key}".encode("utf-8", "surrogatepass"), hashlib.sha256).digest()


def gen_word(salt: bytes, key: str, n: int, counter: int = 0) -> str:
    """Pronounceable lower-case word of exactly n characters."""
    out: list[str] = []
    block = 0
    while len(out) < n:
        d = _digest(salt, key + f"#{block}", counter)
        for b in d:
            out.append(_CONS[b % len(_CONS)] if len(out) % 2 == 0 else _VOW[b % len(_VOW)])
            if len(out) == n:
                break
        block += 1
    return "".join(out)


def apply_case(orig: str, pseudo: str) -> str:
    """Copy the per-character case pattern of orig onto pseudo (same length)."""
    return "".join(p.upper() if o.isupper() else p.lower() for o, p in zip(orig, pseudo))


class WordMapper:
    """token (lower case) -> pseudonym (lower case, same length). Collision-aware when assigned."""

    def __init__(self, salt: bytes, table: dict[str, str] | None = None):
        self.salt = salt
        self.table: dict[str, str] = dict(table or {})
        self._used: set[str] = set(self.table.values())

    # candidate sequence for a token: pool entry first, then generator outputs
    def _candidate(self, tok: str, counter: int) -> str:
        n = len(tok)
        if counter == 0 and n in POOL_BY_LEN:
            pool = POOL_BY_LEN[n]
            return pool[int.from_bytes(_digest(self.salt, tok, 0)[:4], "big") % len(pool)]
        return gen_word(self.salt, tok, n, counter)

    def stateless(self, tok: str) -> str:
        c = self._candidate(tok, 0)
        if c == tok:
            c = self._candidate(tok, 1)
        return c

    def assign(self, tok: str) -> str:
        """Collision-free assignment (phase 1, single process)."""
        if tok in self.table:
            return self.table[tok]
        if len(tok) == 1:
            letters = "abcdefghijklmnopqrstuvwxyz"
            c = letters[int.from_bytes(_digest(self.salt, tok)[:2], "big") % 26]
            if c == tok:
                c = letters[(letters.index(c) + 1) % 26]
            self.table[tok] = c
            return c
        for counter in range(0, 400):
            c = self._candidate(tok, counter)
            if c != tok and c not in self._used:
                self.table[tok] = c
                self._used.add(c)
                return c
        c = self.stateless(tok)  # pathological: very short token in a full namespace
        self.table[tok] = c
        return c

    def get(self, tok: str) -> str:
        t = self.table.get(tok)
        if t is not None:
            return t
        if len(tok) == 1:
            letters = "abcdefghijklmnopqrstuvwxyz"
            c = letters[int.from_bytes(_digest(self.salt, tok)[:2], "big") % 26]
            return c if c != tok else letters[(letters.index(c) + 1) % 26]
        return self.stateless(tok)


class Pseudonymizer:
    """High level string mappers. All return strings of identical length."""

    def __init__(self, salt: bytes, table: dict[str, str] | None = None):
        self.words = WordMapper(salt, table)

    # ---- primitives -----------------------------------------------------------------
    def _digits(self, run: str) -> str:
        d = _digest(self.words.salt, "digits:" + run)
        out = "".join(str(b % 10) for b in d[: len(run)]) if len(run) <= 32 else (
            "".join(str(b % 10) for b in (d * (len(run) // 32 + 1))[: len(run)]))
        if out == run:  # never return the original
            out = str((int(out[0]) + 1) % 10) + out[1:]
        return out

    def _word(self, run: str, keep_words: bool) -> str:
        low = run.lower()
        if keep_words and low in KEEP_WORDS:
            return run
        return apply_case(run, self.words.get(low))

    def map_text(self, s: str, keep_words: bool = True, digits: bool = True) -> str:
        """Map every alphabetic (and optionally digit) run. Non-alphanumerics are preserved."""
        def a(m: re.Match) -> str:
            return self._word(m.group(0), keep_words)
        out = _ALPHA_RUN.sub(a, s)
        if digits:
            out = _DIGIT_RUN.sub(lambda m: self._digits(m.group(0)), out)
        assert len(out) == len(s)
        return out

    # ---- typed mappers ---------------------------------------------------------------
    def person(self, s: str) -> str:
        return self.map_text(s, keep_words=True, digits=False)

    def org(self, s: str) -> str:
        return self.map_text(s, keep_words=True, digits=False)

    def handle(self, s: str) -> str:
        """User names / GitHub owners: map every run of >=3 letters; keep separators and digits-as-is."""
        def a(m: re.Match) -> str:
            r = m.group(0)
            return apply_case(r, self.words.get(r.lower())) if len(r) >= 3 else r
        out = _ALPHA_RUN.sub(a, s)
        assert len(out) == len(s)
        return out

    _SECOND_LEVEL = {"co", "com", "org", "net", "ac", "gov", "edu", "ne", "or"}

    def domain(self, s: str) -> str:
        labels = s.split(".")
        if len(labels) < 2:
            return self.handle(s)
        keep = 1
        if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2].lower() in self._SECOND_LEVEL:
            keep = 2
        mapped = [self.handle(x) if len(x) >= 3 else x for x in labels[: len(labels) - keep]]
        out = ".".join(mapped + labels[len(labels) - keep:])
        assert len(out) == len(s)
        return out

    def email(self, s: str) -> str:
        if "@" not in s:
            return self.map_text(s)
        local, _, dom = s.rpartition("@")
        out = self.map_text(local, keep_words=False, digits=False) + "@" + self.domain(dom)
        assert len(out) == len(s)
        return out

    def text_like(self, s: str, kind: str) -> str:
        fn = {"person": self.person, "org": self.org, "handle": self.handle, "domain": self.domain,
              "email": self.email}.get(kind, self.person)
        return fn(s)


# ---- helpers shared by harvesters/rewriters --------------------------------------------------
def is_placeholder(s: str) -> bool:
    t = re.sub(r"\s+", " ", s.strip().strip("'\"").strip()).lower()
    if t in PLACEHOLDERS:
        return True
    if not re.search(r"[^\W\d_]", t):  # no letters at all
        return True
    if len(t) < 3:
        return True
    return False


def is_vendor_only(s: str) -> bool:
    toks = [t.lower() for t in _ALPHA_RUN.findall(s)]
    return bool(toks) and all(t in VENDOR_WORDS or t in KEEP_WORDS for t in toks)


def identity_candidate(s: str) -> bool:
    """True if s looks like a real person/org string worth harvesting."""
    if is_placeholder(s) or is_vendor_only(s):
        return False
    if len(s) > 120:
        return False
    return True
