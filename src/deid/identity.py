"""Identity store (harvested phrases) + length-preserving text replacer.

Two sources of replacements:
  1. PHRASES  - exact person / organisation / handle / domain strings harvested from identity-bearing
                metadata fields (phase 1). Matching is case-insensitive and token-boundary aware.
  2. PATTERNS - structural identifiers found anywhere in text: e-mail addresses, home-directory user names
                (C:\\Users\\<name>\\, /home/<name>/, /Users/<name>/) and GitHub/GitLab owner segments.

Every replacement preserves the character length of what it replaces.
"""
from __future__ import annotations

import json
import os
import re
import secrets
from dataclasses import dataclass, field

from .names import KEEP_WORDS, Pseudonymizer, identity_candidate, is_placeholder

_TLDS = ("com|org|net|edu|gov|io|de|uk|fr|cn|jp|ru|it|es|nl|ch|at|se|no|dk|fi|pl|cz|hu|pt|br|in|kr|tw|hk|sg|au|"
         "ca|nz|eu|info|biz|co|me|us|xyz|dev|app|tech|ai|cc|tv|ly|be|ie|il|za|mx|ar|cl|tr|ua|ro|gr|sk|si|hr|rs|"
         "bg|lt|lv|ee|is|lu|th|vn|my|id|ph|pk|bd|cat|name|pro|mil|int|asia|club|online|site|top|cloud")
EMAIL_RE = re.compile(
    r"(?<![A-Za-z0-9._%+\-])[A-Za-z0-9._%+\-]{1,64}@(?:[A-Za-z0-9\-]+\.)+(?:" + _TLDS + r")(?![A-Za-z0-9])",
    re.I)
HOME_WIN_RE = re.compile(r"(?i)([A-Za-z]:[\\/]{1,4}Users[\\/]{1,4})([^\\/:*?\"<>|\r\n';,()]{2,40})(?=[\\/])")
HOME_NIX_RE = re.compile(r"(?i)(?<![A-Za-z0-9_.])(/(?:home|Users)/)([A-Za-z0-9][A-Za-z0-9_.\-]{1,31})(?=/)")
GIT_RE = re.compile(r"(?i)((?:github|gitlab)\.com[/:])([A-Za-z0-9][A-Za-z0-9_.\-]{0,38})(?=[/\s\"'<>)\]\\]|$)")
GHIO_RE = re.compile(r"(?i)(?<![A-Za-z0-9\-])([A-Za-z0-9][A-Za-z0-9\-]{2,38})(\.github\.io)")
SKIP_EMAIL_DOMAINS = {"example.com", "example.org", "example.net", "localhost", "test.com", "domain.com"}
SKIP_HOME = {"public", "default", "all users", "administrator", "admin", "user", "users", "guest", "shared",
             "owner", "root", "runner", "vsts", "build", "work", "workspace", "appdata", "home", "pc",
             "username", "yourname", "name", "myuser", "default user", "defaultuser0", "docker", "ubuntu",
             "vagrant", "travis", "jenkins", "circleci", "builder", "buildbot", "azureuser", "ec2-user"}
SKIP_GIT_OWNER = {"orgs", "topics", "sponsors", "features", "about", "settings", "marketplace", "search",
                  "login", "join", "pricing", "explore", "collections", "trending", "notifications", "pulls",
                  "issues", "apps", "enterprise", "security", "customer-stories", "readme"}

# English / CAD words that must never be treated as a single-token identity.
COMMON_WORDS = set("""part parts assembly model models drawing drawings robot robots design designs test tests project
projects mechanical engineering bracket plate frame base cover shaft gear gears sheet metal body main final draft
default student teacher university college school institute lab laboratory department team group club
open source hardware software library standard custom generic template sample example demo unknown none
user users admin author owner company corporation systems system technology technologies solutions
industries industrial manufacturing research development products product tools tool machine machines
motor motors screw screws bolt bolts nut nuts washer washers bearing bearings spring springs housing
mount holder support arm leg wheel wheels lid box tube pipe rod bar beam block disk disc ring cap pin
""".split())

_TOK = re.compile(r"[A-Za-z0-9]+")


def _tokens(s: str) -> list[str]:
    return _TOK.findall(s)


def load_or_create_salt(path: str) -> bytes:
    if os.path.exists(path):
        with open(path, "rb") as f:
            return f.read().strip()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    salt = secrets.token_hex(32).encode()
    with open(path, "wb") as f:
        f.write(salt)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return salt


@dataclass
class IdentityStore:
    salt: bytes
    phrases: dict = field(default_factory=dict)  # normalised phrase (lower, single spaces) -> kind
    counts: dict = field(default_factory=dict)  # phrase -> number of files it was harvested from
    owners: set = field(default_factory=set)    # GitHub/GitLab owner handles seen in the folder layout (lower case)
    pz: Pseudonymizer | None = None

    def __post_init__(self):
        if self.pz is None:
            self.pz = Pseudonymizer(self.salt)

    # ---- building -------------------------------------------------------------------
    @staticmethod
    def norm(s: str) -> str:
        return re.sub(r"\s+", " ", s.strip().strip("'\"").strip())

    def add(self, kind: str, s: str, weight: int = 1) -> bool:
        s = self.norm(s)
        if kind == "owner":
            # Every owner folder name goes through collision-free assignment (so two owners can never be
            # merged into one output folder) ...
            if len(s) >= 3:
                self.owners.add(s.lower())
            # ... but only distinctive handles become global text phrases (avoid replacing common words).
            kind = "handle"
            if not (len(s) >= 7 or re.search(r"[\d_\-]", s)):
                return True
        if not s or not identity_candidate(s):
            return False
        toks = _tokens(s)
        if kind in ("person", "org") and not any(len(t) >= 2 and t.lower() not in KEEP_WORDS for t in toks):
            return False
        if len(toks) == 1:
            t = toks[0].lower()
            if len(t) < 4 or t in COMMON_WORDS:
                return False
        for variant in {s, s.encode("utf-8").decode("latin-1")}:
            key = variant.lower()
            if key not in self.phrases:
                self.phrases[key] = kind
            self.counts[key] = self.counts.get(key, 0) + weight
        return True

    def finalize(self) -> None:
        """Assign collision-free word pseudonyms (deterministic order)."""
        words: set[str] = set()
        for key, kind in self.phrases.items():
            runs = re.findall(r"[^\W\d_]+", key, re.U)
            if kind == "domain":
                runs = runs[:-1] if len(runs) > 1 else runs
            for r in runs:
                if kind in ("person", "org") and (r in KEEP_WORDS):
                    continue
                if kind in ("handle", "domain", "email") and len(r) < 3:
                    continue
                words.add(r)
        for o in self.owners:
            for r in re.findall(r"[^\W\d_]+", o, re.U):
                if len(r) >= 3:
                    words.add(r)
        for w in sorted(words, key=lambda x: (-len(x), x)):
            self.pz.words.assign(w)

    # ---- persistence ----------------------------------------------------------------
    def save(self, path: str) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"phrases": self.phrases, "counts": self.counts, "owners": sorted(self.owners),
                       "words": self.pz.words.table}, f,
                      ensure_ascii=False, indent=0, sort_keys=True)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    @classmethod
    def load(cls, path: str, salt: bytes) -> "IdentityStore":
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        st = cls(salt=salt, phrases=d["phrases"], counts=d.get("counts", {}), owners=set(d.get("owners", [])))
        st.pz = Pseudonymizer(salt, d.get("words", {}))
        return st

    def replacer(self) -> "Replacer":
        return Replacer(self)


class Replacer:
    """Length-preserving replacement of harvested phrases and structural identifiers."""

    def __init__(self, store: IdentityStore):
        self.store = store
        self.pz = store.pz
        self.index: dict[str, list[tuple[re.Pattern, str]]] = {}
        for key, kind in store.phrases.items():
            toks = _tokens(key)
            if not toks:
                continue
            # the regex is compiled lazily (60k+ identities would otherwise cost seconds and ~GB per worker)
            self.index.setdefault(toks[0].lower(), []).append([toks, kind, len(toks), None])  # type: ignore
        for v in self.index.values():
            v.sort(key=lambda x: -x[2])  # type: ignore
        self.hits: dict[str, int] = {}

    # ---- structural patterns -----------------------------------------------------------
    def _patterns(self, s: str) -> str:
        pz = self.pz
        if "@" in s:
            def e(m: re.Match) -> str:
                dom = m.group(0).rpartition("@")[2].lower()
                if dom in SKIP_EMAIL_DOMAINS:
                    return m.group(0)
                return pz.email(m.group(0))
            s = EMAIL_RE.sub(e, s)
        low = s.lower()
        if "users" in low and ":" in s:
            s = HOME_WIN_RE.sub(lambda m: m.group(1) + (m.group(2) if is_placeholder(m.group(2)) or
                                m.group(2).lower() in SKIP_HOME else pz.handle(m.group(2))), s)
        if "/home/" in low or "/users/" in low:
            s = HOME_NIX_RE.sub(lambda m: m.group(1) + (m.group(2) if m.group(2).lower() in SKIP_HOME
                                else pz.handle(m.group(2))), s)
        if "github" in low or "gitlab" in low:
            s = GIT_RE.sub(lambda m: m.group(1) + (m.group(2) if m.group(2).lower() in SKIP_GIT_OWNER
                           or len(m.group(2)) < 3 else pz.handle(m.group(2))), s)
            s = GHIO_RE.sub(lambda m: pz.handle(m.group(1)) + m.group(2), s)
        return s

    # ---- phrase matching -----------------------------------------------------------------
    def _phrases(self, s: str) -> str:
        if not self.index:
            return s
        out: list[str] = []
        last = 0
        for m in _TOK.finditer(s):
            if m.start() < last:
                continue
            cands = self.index.get(m.group(0).lower())
            if not cands:
                continue
            for cand in cands:
                toks, kind, _n, pat = cand
                if pat is None:
                    pat = cand[3] = re.compile(r"[\s,._\-]{1,3}".join(re.escape(t) for t in toks) + r"(?![A-Za-z0-9])", re.I)
                mm = pat.match(s, m.start())
                if mm:
                    span = mm.group(0)
                    out.append(s[last:m.start()])
                    out.append(self.pz.text_like(span, kind))
                    last = mm.end()
                    self.hits[kind] = self.hits.get(kind, 0) + 1
                    break
        if not out:
            return s
        out.append(s[last:])
        return "".join(out)

    def replace(self, s: str) -> str:
        r = self._phrases(self._patterns(s))
        assert len(r) == len(s), (len(r), len(s))
        return r

    def replace_bytes(self, b: bytes) -> bytes:
        """Byte-exact variant (latin-1 view); length always preserved."""
        s = b.decode("latin-1")
        return self.replace(s).encode("latin-1")

    # ---- path mapping -------------------------------------------------------------------
    _OWNER_SEP = "__"

    def map_component(self, comp: str, *, owner_dir: bool = False) -> str:
        """Map one path component. Extension is never touched. owner_dir -> '<owner>__<repo>' GitHub layout."""
        if not comp or comp in (".", ".."):
            return comp
        stem, ext = comp, ""
        # Keep the final extension verbatim ("part.step", "x.tar.gz" -> only ".gz" is protected; a
        # preceding ".tar" contains no identity).
        dot = comp.rfind(".")
        if 0 < dot and len(comp) - dot <= 12 and comp[dot + 1:].isalnum():
            stem, ext = comp[:dot], comp[dot:]
        if owner_dir and self._OWNER_SEP in stem:
            owner, sep, rest = stem.partition(self._OWNER_SEP)
            if len(owner) >= 3 and owner.lower() not in SKIP_GIT_OWNER:
                owner = self.pz.handle(owner)
            stem = owner + sep + self.replace(rest)
        else:
            stem = self.replace(stem)
        out = stem + ext
        assert len(out) == len(comp)
        return out

    def map_path(self, rel: str, *, rename_owners: bool = True) -> str:
        """Map a '/'-separated relative path (input layout: sources/<origin>/<owner>__<repo>/...)."""
        parts = rel.split("/")
        out = []
        for i, p in enumerate(parts):
            owner_dir = rename_owners and (
                (i == 2 and parts[0] == "sources" and parts[1] in ("github", "gitlab")) or
                (i == 1 and parts[0] == "archives" and False))
            if i == 1 and parts[0] == "archives":
                owner_dir = rename_owners
            out.append(self.map_component(p, owner_dir=owner_dir))
        return "/".join(out)
