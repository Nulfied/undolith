import pytest

from undolith import Undolith
from undolith.adapters import FileSystem


@pytest.fixture
def workspace(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


@pytest.fixture
def guard(tmp_path, workspace):
    g = Undolith(tmp_path / ".undolith")
    g.register(FileSystem(workspace))
    yield g
    g.ledger.store.close()
