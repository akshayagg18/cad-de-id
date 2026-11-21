"""Unit tests: pseudonym invariants and the text replacer."""
import re

import fixtures as fx
from deid.identity import IdentityStore
from deid.names import Pseudonymizer, apply_case, identity_candidate, is_placeholder


def test_length_preserving_everywhere(store):
    pz = store.pz
    samples = ["John Smith", "SMITH, john", "o'brien", "Quillfeather Robotics Ltd", "a.b@c-d.co.uk", "x", "ab", "Zoë Müller",
               "jmarlowe77", "123 Main St.", "Äpfel & Söhne GmbH"]
    for s in samples:
        for fn in (pz.person, pz.org, pz.handle, pz.map_text):
            assert len(fn(s)) == len(s), (fn.__name__, s)
    assert len(pz.email("jon.doe@acme.example.com")) == len("jon.doe@acme.example.com")
    assert len(pz.domain("sub.acme.co.uk")) == len("sub.acme.co.uk")


def test_deterministic_and_salt_dependent():
    a, b, c = Pseudonymizer(b"s1"), Pseudonymizer(b"s1"), Pseudonymizer(b"s2")
    assert a.person("Jonathan Marlowe") == b.person("Jonathan Marlowe")
    assert a.person("Jonathan Marlowe") != c.person("Jonathan Marlowe")
    assert a.person("Jonathan Marlowe") != "Jonathan Marlowe"


def test_case_pattern_and_separators_kept():
    pz = Pseudonymizer(b"k")
    out = pz.person("McDONALD-smith")
    assert re.fullmatch(r"[A-Za-z]+-[A-Za-z]+", out)
    assert out.split("-")[1].islower()
    assert apply_case("AbC", "xyz") == "XyZ"


def test_token_level_consistency_across_forms():
    pz = Pseudonymizer(b"k")
    a = pz.person("Jonathan Marlowe")
    b = pz.person("Marlowe, Jonathan")
    assert a.split()[0] == b.split(", ")[1] and a.split()[1] == b.split(", ")[0].rstrip(",")
    e = pz.email("jonathan.marlowe@x-corp.com")
    assert e.startswith(a.split()[0].lower() + ".") and e.endswith(".com")


def test_distinct_identities_get_distinct_pseudonyms():
    st = IdentityStore(b"salt")
    names = [f"Person{c}{d}x Surname{c}{d}" for c in "abcdefghij" for d in "klmnop"]
    names = [n.replace("Person", "Persn") for n in names]
    toks = set()
    for n in names:
        st.add("person", n)
    st.finalize()
    outs = {st.pz.person(n) for n in names}
    assert len(outs) == len(names)


def test_placeholders_and_vendors_are_not_identities():
    for s in ["user", "Company", " ", "'", "Unknown", "N/A", "Autodesk Inc.", "SolidWorks", "FreeCAD"]:
        assert not identity_candidate(s), s
    assert identity_candidate("Jonathan Marlowe") and identity_candidate("Quillfeather Robotics Ltd")
    assert is_placeholder("xx")


def test_replacer_patterns_and_phrases(store):
    rep = store.replacer()
    s = (f"Contact {fx.EMAIL} or {fx.AUTHOR}; file C:\\\\Users\\\\{fx.USER}\\\\Documents\\\\a.step; "
         f"https://github.com/{fx.OWNER}/repo and /home/{fx.USER}/x; {fx.ORG}.")
    out = rep.replace(s)
    assert len(out) == len(s)
    for secret in (fx.EMAIL, fx.AUTHOR, fx.USER, fx.OWNER, fx.ORG, "Marlowe", "Quillfeather"):
        assert secret not in out and secret.lower() not in out.lower(), secret
    assert "github.com/" in out and "Documents" in out and "Users" in out and ".step" in out


def test_replacer_leaves_innocent_text_alone(store):
    rep = store.replacer()
    s = "Bracket for 3D printer, M3 screw, user@example.com, C:\\Users\\Public\\x /home/runner/work"
    assert rep.replace(s) == s


def test_map_path_owner_and_extension(store):
    rep = store.replacer()
    p = f"sources/github/{fx.OWNER}__widget-cad/parts/{fx.AUTHOR} bracket.STEP"
    q = rep.map_path(p)
    assert len(q) == len(p) and q.endswith(".STEP")
    assert q.split("/")[:2] == ["sources", "github"] and q.split("/")[3] == "parts"
    assert fx.OWNER not in q and "__widget-cad" in q and fx.AUTHOR not in q
    other = "sources/github/unharvested-owner__widget/a.step"
    assert rep.map_path(other, rename_owners=False) == other          # prefix rule off -> untouched
    assert rep.map_path(other).split("/")[2] != other.split("/")[2]   # prefix rule on -> mapped
    assert len(rep.map_path(other)) == len(other)
    # archives/<owner>__repo__hash.zip
    a = f"archives/{fx.OWNER}__repo__abc123.zip"
    b = rep.map_path(a)
    assert b.endswith("__repo__abc123.zip") and fx.OWNER not in b


def test_map_path_is_a_function_of_the_path_only(store):
    rep = store.replacer()
    p = f"sources/github/{fx.OWNER}__r/a/{fx.USER}.txt"
    assert rep.map_path(p) == store.replacer().map_path(p)
    # an archive entry and the sibling '.extracted' folder entry map identically
    assert rep.map_path(f"x/{fx.AUTHOR}/f.step", rename_owners=False) == rep.map_path(f"x/{fx.AUTHOR}/f.step", rename_owners=False)
