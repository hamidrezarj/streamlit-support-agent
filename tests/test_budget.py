import json
import pytest
from ssa.llm import client as L


def test_budget_guard_blocks_before_sending(tmp_path, monkeypatch):
    monkeypatch.setattr(L, "LEDGER", tmp_path / "ledger.jsonl")
    (tmp_path / "ledger.jsonl").write_text(json.dumps({"usd": 4.99}) + "\n")
    monkeypatch.setenv("LLM_BASE_URL", "http://invalid.local")
    monkeypatch.setenv("LLM_API_KEY", "x")
    monkeypatch.setenv("BUDGET_USD", "5.0")
    c = L.LLMClient()
    with pytest.raises(L.BudgetExceeded):
        c.embed(["a" * 3_000_000])          # ~$0.02 estimate > remaining $0.01; no HTTP call made


def test_missing_key_is_explicit(monkeypatch):
    monkeypatch.setattr(L.C, "load_dotenv", lambda *a, **k: None)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    with pytest.raises(L.LLMError):
        L.LLMClient()
