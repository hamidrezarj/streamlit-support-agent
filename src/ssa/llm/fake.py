"""Scripted stand-in for LLMClient.chat / chat_json - for offline logic tests.

    fake = FakeLLM([{"outcome": "info_request"}, "plain text", tool_call("search", {"q": "x"})])
    fake = FakeLLM(lambda messages, **kw: {"answer": 42})      # or a function

Each queued item is returned by one call: dict -> JSON content, str -> text,
ToolCalls -> assistant tool calls. Every request is kept in `fake.calls`.
No network, no ledger, no cost.
"""
from __future__ import annotations

import json
from typing import Any, Callable

from .client import ChatResult, LLMError


class ToolCalls(list):
    pass


def tool_call(name: str, args: dict, call_id: str | None = None) -> ToolCalls:
    return ToolCalls([{"id": call_id or f"call_{name}", "type": "function",
                       "function": {"name": name, "arguments": json.dumps(args)}}])


class FakeLLM:
    def __init__(self, script: list | Callable[..., Any], model: str = "fake"):
        self.script = script
        self.chat_model = model
        self.calls: list[dict] = []
        self.stats = {"live_calls": 0, "replayed_calls": 0, "tokens_in": 0, "tokens_out": 0,
                      "usd_live": 0.0, "usd_replayed": 0.0, "latency_s": 0.0}

    def reset_stats(self) -> None:
        for k in self.stats:
            self.stats[k] = 0 if isinstance(self.stats[k], int) else 0.0

    def _next(self, messages, **kw):
        if callable(self.script):
            return self.script(messages, **kw)
        if not self.script:
            raise LLMError("FakeLLM: script exhausted")
        return self.script.pop(0)

    def chat(self, messages: list[dict], **kw) -> ChatResult:
        self.calls.append({"messages": messages, **kw})
        item = self._next(messages, **kw)
        if isinstance(item, Exception):
            raise item
        tool_calls = list(item) if isinstance(item, ToolCalls) else []
        content = None if tool_calls else (json.dumps(item) if isinstance(item, dict) else str(item))
        msg = {"role": "assistant", "content": content}
        if tool_calls:
            msg["tool_calls"] = tool_calls
        self.stats["live_calls"] += 1
        return ChatResult(content=content, tool_calls=tool_calls, message=msg, model=self.chat_model,
                          tokens_in=0, tokens_out=0, usd=0.0, latency_s=0.0, replayed=False, key="fake")

    def chat_json(self, messages: list[dict], required: list[str] | None = None, **kw) -> dict:
        res = self.chat(messages, **kw)
        data = json.loads(res.content)
        missing = [k for k in (required or []) if k not in data]
        if missing:
            raise LLMError(f"FakeLLM JSON missing keys {missing}")
        return data
