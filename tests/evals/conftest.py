"""Historical-data dependency supplied explicitly to archive-only tests."""
import shutil
import uuid

import pytest

from evals.common import ROOT
from evals.history_fixtures import unpack_history


@pytest.fixture(scope="module")
def history_tree():
    root = ROOT / ".tmp" / ("engineering-history-" + uuid.uuid4().hex)
    unpack_history(root)
    try:
        yield root
    finally:
        shutil.rmtree(root)
