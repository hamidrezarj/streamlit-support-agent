import pytest
from ssa.llm import cache as LC, client as L


@pytest.fixture(autouse=True)
def _isolate_llm_files(tmp_path, monkeypatch):
    """Tests never touch the real ledger / chat cache."""
    monkeypatch.setattr(L, "LEDGER", tmp_path / "ledger.jsonl")
    monkeypatch.setattr(LC, "CACHE_PATH", tmp_path / "chat.sqlite")
