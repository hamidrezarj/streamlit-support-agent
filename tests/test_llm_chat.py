"""Offline tests for chat(): ledger, budget guard, record/replay cache, JSON repair, fake client."""
import json

import pytest
from ssa.llm import client as L
from ssa.llm.cache import ReplayMiss
from ssa.llm.fake import FakeLLM, tool_call


def _resp(content, tin=100, tout=20, tool_calls=None):
    msg = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return {"model": "gpt-4o-mini", "choices": [{"message": msg, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": tin, "completion_tokens": tout}}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(L.C, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("LLM_BASE_URL", "http://invalid.local")
    monkeypatch.setenv("LLM_API_KEY", "sk-SECRET-123")
    monkeypatch.setenv("BUDGET_USD", "5.0")
    monkeypatch.setenv("CHAT_MODEL", "gpt-4o-mini")
    monkeypatch.setenv("LLM_CACHE_MODE", "cache")
    c = L.LLMClient()
    c.sent = []

    def fake_post(path, payload):
        c.sent.append((path, payload))
        return c.next_response.pop(0)
    c._post = fake_post
    return c


MSG = [{"role": "user", "content": "hi"}]


def test_live_call_is_ledgered_then_replayed_for_free(client):
    client.next_response = [_resp("hello")]
    r1 = client.chat(MSG, tag="t")
    assert (r1.content, r1.replayed) == ("hello", False)
    assert r1.usd == pytest.approx(100 / 1e6 * 0.15 + 20 / 1e6 * 0.60)
    led = [json.loads(l) for l in L.LEDGER.read_text().splitlines()]
    assert len(led) == 1 and led[0]["kind"] == "chat" and led[0]["tokens_in"] == 100
    assert "SECRET" not in L.LEDGER.read_text()                    # key never logged ...
    assert b"SECRET" not in client.cache.path.read_bytes()         # ... nor cached
    r2 = client.chat(MSG, tag="t")                                 # same request -> cache
    assert r2.replayed and r2.content == "hello" and len(client.sent) == 1
    assert len(L.LEDGER.read_text().splitlines()) == 1             # replay not billed
    assert client.stats["live_calls"] == 1 and client.stats["replayed_calls"] == 1


def test_different_request_is_a_cache_miss(client):
    client.next_response = [_resp("a"), _resp("b")]
    client.chat(MSG)
    assert client.chat(MSG, temperature=0.5).content == "b"


def test_replay_mode_never_calls_network(client, monkeypatch):
    client.next_response = [_resp("rec")]
    client.chat(MSG)
    monkeypatch.setenv("LLM_CACHE_MODE", "replay")
    monkeypatch.delenv("LLM_API_KEY")                              # replay needs no key
    c2 = L.LLMClient()
    c2._post = lambda *a: pytest.fail("network used in replay mode")
    assert c2.chat(MSG).replayed
    with pytest.raises(ReplayMiss):
        c2.chat([{"role": "user", "content": "never recorded"}])


def test_live_mode_bypasses_cache(client):
    client.next_response = [_resp("old"), _resp("new")]
    client.chat(MSG)
    client.cache_mode = "live"
    assert client.chat(MSG).content == "new" and len(client.sent) == 2


def test_budget_blocks_chat_before_sending(client):
    L.LEDGER.write_text(json.dumps({"usd": 4.9999}) + "\n")
    client._post = lambda *a: pytest.fail("sent despite budget")
    with pytest.raises(L.BudgetExceeded):
        client.chat(MSG, max_tokens=2000)                          # 2000 out tokens ~ $0.0012


def test_tool_calls_are_returned(client):
    tc = [{"id": "c1", "type": "function", "function": {"name": "search", "arguments": "{\"q\":\"x\"}"}}]
    client.next_response = [_resp(None, tool_calls=tc)]
    r = client.chat(MSG, tools=[{"type": "function", "function": {"name": "search", "parameters": {}}}])
    assert r.tool_calls[0]["function"]["name"] == "search" and r.message["tool_calls"] == tc
    assert client.sent[0][1]["tools"]


def test_chat_json_repairs_once(client):
    client.next_response = [_resp("not json"), _resp('{"outcome": "info_request"}')]
    out = client.chat_json(MSG, required=["outcome"])
    assert out == {"outcome": "info_request"}
    assert client.sent[0][1]["response_format"] == {"type": "json_object"}
    assert "not usable" in client.sent[1][1]["messages"][-1]["content"]


def test_chat_json_gives_up_after_repair(client):
    client.next_response = [_resp('{"a": 1}'), _resp('{"a": 2}')]
    with pytest.raises(L.LLMError, match="missing keys"):
        client.chat_json(MSG, required=["outcome"])


def test_fake_llm_scripted():
    f = FakeLLM([{"outcome": "escalate"}, tool_call("read_case", {"id": 1}), RuntimeError("boom")])
    assert f.chat_json(MSG, required=["outcome"])["outcome"] == "escalate"
    r = f.chat(MSG)
    assert r.content is None and json.loads(r.tool_calls[0]["function"]["arguments"]) == {"id": 1}
    with pytest.raises(RuntimeError):
        f.chat(MSG)
    assert len(f.calls) == 3
