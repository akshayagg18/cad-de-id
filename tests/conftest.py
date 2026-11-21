import os
import sys

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, "..", "src"))
sys.path.insert(0, HERE)

import pytest  # noqa: E402

import fixtures as fx  # noqa: E402
from deid.identity import IdentityStore  # noqa: E402
from deid.rewriters import Ctx  # noqa: E402

SALT = b"unit-test-salt-not-secret"


@pytest.fixture()
def store():
    st = IdentityStore(SALT)
    st.add("person", fx.AUTHOR)
    st.add("org", fx.ORG)
    st.add("handle", fx.OWNER)
    st.add("handle", fx.USER)
    st.finalize()
    return st


@pytest.fixture()
def ctx(store):
    return Ctx(rep=store.replacer(), pz=store.pz)
