"""OpenAI-compatible client for the Metis gateway, with a cost ledger + budget guard.

* Config from .env: LLM_BASE_URL, LLM_API_KEY, EMBED_MODEL, CHAT_MODEL, BUDGET_USD.
* Every real call appends one line to data/usage/ledger.jsonl
  (time, kind, model, tokens, estimated USD) - this is the cost report source.
* Before a call, the guard estimates its cost; if ledger + estimate > budget,
  BudgetExceeded is raised and nothing is sent (state stays intact).
* The API key is only placed in the Authorization header; never logged.
* chat()/chat_json() go through the record/replay cache (llm/cache.py):
  replayed answers cost $0, are NOT written to the ledger and are flagged
  `replayed=True` so reports can separate them from real calls.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field

import requests

from ..data import config as C
from ..data.io_utils import utcnow

log = logging.getLogger("llm")
LEDGER = C.DATA / "usage" / "ledger.jsonl"

# USD per 1M tokens (OpenAI list prices; Metis may differ -> override via env)
PRICES = {
    "text-embedding-3-small": (0.02, 0.0),
    "text-embedding-3-large": (0.13, 0.0),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1-nano": (0.10, 0.40),
}


class BudgetExceeded(RuntimeError):
    pass


class LLMError(RuntimeError):
    pass


@dataclass
class ChatResult:
    content: str | None                 # assistant text (None if only tool calls)
    tool_calls: list[dict]              # OpenAI-format tool calls ([] if none)
    message: dict                       # assistant message, ready to append to history
    model: str
    tokens_in: int
    tokens_out: int
    usd: float                          # cost of the (original) live call
    latency_s: float                    # latency of the (original) live call
    replayed: bool                      # True -> served from cache, $0 now
    key: str                            # cache key of the request
    finish_reason: str | None = None


def price(model: str) -> tuple[float, float]:
    key = "PRICE_" + model.upper().replace("-", "_").replace(".", "_")
    pin, pout = PRICES.get(model, (1.0, 4.0))          # unknown model -> pessimistic
    return float(os.environ.get(key + "_IN", pin)), float(os.environ.get(key + "_OUT", pout))


def spent_usd() -> float:
    if not LEDGER.exists():
        return 0.0
    return sum(json.loads(l).get("usd", 0.0) for l in LEDGER.open() if l.strip())


class LLMClient:
    def __init__(self):
        C.load_dotenv()
        self.base = os.environ.get("LLM_BASE_URL", "").rstrip("/")
        self._key = os.environ.get("LLM_API_KEY", "")
        self.embed_model = os.environ.get("EMBED_MODEL", "text-embedding-3-small")
        self.chat_model = os.environ.get("CHAT_MODEL", "gpt-4o-mini")
        self.budget = float(os.environ.get("BUDGET_USD", "5.0"))
        from .cache import MODES, ChatCache
        self.cache_mode = os.environ.get("LLM_CACHE_MODE", "cache")
        if self.cache_mode not in MODES:
            raise LLMError(f"LLM_CACHE_MODE must be one of {MODES}")
        self.cache = ChatCache() if self.cache_mode != "off" else None
        self.stats = {}
        self.reset_stats()
        if self.cache_mode != "replay" and (not self.base or not self._key):
            raise LLMError("LLM_BASE_URL / LLM_API_KEY missing - fill them in .env")
        self._s = requests.Session()
        self._s.headers.update({"Authorization": f"Bearer {self._key}",
                                "Content-Type": "application/json"})

    # ------------------------------------------------------------ guard/ledger
    def guard(self, est_usd: float) -> None:
        spent = spent_usd()
        if spent + est_usd > self.budget:
            raise BudgetExceeded(f"budget ${self.budget:.2f}: spent ${spent:.4f}, "
                                 f"next call ~${est_usd:.4f}")

    def _record(self, kind: str, model: str, tin: int, tout: int, **extra) -> float:
        pin, pout = price(model)
        usd = tin / 1e6 * pin + tout / 1e6 * pout
        LEDGER.parent.mkdir(parents=True, exist_ok=True)
        with LEDGER.open("a") as f:
            f.write(json.dumps({"at": utcnow(), "kind": kind, "model": model,
                                "tokens_in": tin, "tokens_out": tout,
                                "usd": round(usd, 6), **extra}) + "\n")
        return usd

    def _post(self, path: str, payload: dict) -> dict:
        for attempt in range(5):
            try:
                r = self._s.post(self.base + path, json=payload, timeout=120)
            except requests.RequestException as e:
                log.warning("network error %s", type(e).__name__)
                time.sleep(2 ** attempt)
                continue
            if r.status_code == 200:
                return r.json()
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(float(r.headers.get("Retry-After", 2 ** attempt)))
                continue
            raise LLMError(f"POST {path} -> HTTP {r.status_code}: {r.text[:300]}")
        raise LLMError(f"POST {path}: retries exhausted")

    # ------------------------------------------------------------ embeddings
    def embed(self, texts: list[str], model: str | None = None, tag: str = "") -> list[list[float]]:
        model = model or self.embed_model
        est_tokens = sum(len(t) for t in texts) // 3          # conservative
        self.guard(est_tokens / 1e6 * price(model)[0])
        data = self._post("/embeddings", {"model": model, "input": texts})
        vecs = [d["embedding"] for d in sorted(data["data"], key=lambda d: d["index"])]
        used = (data.get("usage") or {}).get("prompt_tokens", est_tokens)
        self._record("embedding", model, used, 0, n_inputs=len(texts), tag=tag)
        return vecs

    # ------------------------------------------------------------ chat
    def reset_stats(self) -> None:
        """Per-run counters (live vs replayed kept apart for the cost report)."""
        self.stats = {"live_calls": 0, "replayed_calls": 0, "tokens_in": 0, "tokens_out": 0,
                      "usd_live": 0.0, "usd_replayed": 0.0, "latency_s": 0.0}

    def _account(self, r: ChatResult) -> None:
        st = self.stats
        st["replayed_calls" if r.replayed else "live_calls"] += 1
        st["tokens_in"] += r.tokens_in
        st["tokens_out"] += r.tokens_out
        st["usd_replayed" if r.replayed else "usd_live"] += r.usd
        st["latency_s"] += r.latency_s

    def chat(self, messages: list[dict], *, model: str | None = None, tools: list[dict] | None = None,
             tool_choice: str | dict | None = None, json_mode: bool = False,
             temperature: float = 0.0, max_tokens: int = 1200, seed: int | None = 7,
             tag: str = "") -> ChatResult:
        """One chat completion. Budget-guarded, ledgered, cached (see cache.py)."""
        from .cache import ReplayMiss, request_key
        model = model or self.chat_model
        req = {"model": model, "messages": messages, "temperature": temperature,
               "max_tokens": max_tokens}
        if seed is not None:
            req["seed"] = seed
        if tools:
            req["tools"] = tools
        if tool_choice is not None:
            req["tool_choice"] = tool_choice
        if json_mode:
            req["response_format"] = {"type": "json_object"}
        key = request_key(req)

        if self.cache is not None and self.cache_mode in ("cache", "replay"):
            hit = self.cache.get(key)
            if hit:
                r = self._to_result(hit["response"], model, hit["tokens_in"], hit["tokens_out"],
                                    hit["usd"], hit["latency_s"], True, key)
                self._account(r)
                return r
            if self.cache_mode == "replay":
                raise ReplayMiss(f"no recorded response for request {key[:12]} (tag={tag!r})")

        pin, pout = price(model)
        est_in = len(json.dumps(messages)) // 3 + (len(json.dumps(tools)) // 3 if tools else 0)
        self.guard(est_in / 1e6 * pin + max_tokens / 1e6 * pout)
        t0 = time.monotonic()
        data = self._post("/chat/completions", req)
        latency = round(time.monotonic() - t0, 3)
        u = data.get("usage") or {}
        tin, tout = u.get("prompt_tokens", est_in), u.get("completion_tokens", 0)
        usd = self._record("chat", model, tin, tout, tag=tag, latency_s=latency, key=key[:12])
        if self.cache is not None:
            self.cache.put(key, req, data, tin, tout, usd, latency, tag)
        r = self._to_result(data, model, tin, tout, usd, latency, False, key)
        self._account(r)
        return r

    @staticmethod
    def _to_result(data: dict, model: str, tin: int, tout: int, usd: float, latency: float,
                   replayed: bool, key: str) -> ChatResult:
        try:
            choice = data["choices"][0]
            msg = choice["message"]
        except (KeyError, IndexError, TypeError):
            raise LLMError(f"malformed chat response: {str(data)[:200]}")
        clean = {"role": "assistant", "content": msg.get("content")}
        if msg.get("tool_calls"):
            clean["tool_calls"] = msg["tool_calls"]
        return ChatResult(content=msg.get("content"), tool_calls=msg.get("tool_calls") or [],
                          message=clean, model=data.get("model", model), tokens_in=tin,
                          tokens_out=tout, usd=round(usd, 6), latency_s=latency, replayed=replayed,
                          key=key, finish_reason=choice.get("finish_reason"))

    def chat_json(self, messages: list[dict], required: list[str] | None = None,
                  repair: bool = True, **kw) -> dict:
        """chat() in JSON mode -> parsed dict. One repair turn on invalid JSON /
        missing keys, then LLMError (callers decide how to degrade)."""
        res = self.chat(messages, json_mode=True, **kw)
        err = _json_problem(res.content, required)
        if err and repair:
            fix = messages + [res.message, {"role": "user", "content":
                  f"Your reply was not usable ({err}). Return ONLY one valid JSON object"
                  + (f" with keys {required}." if required else ".")}]
            res = self.chat(fix, json_mode=True, **{**kw, "tag": (kw.get("tag") or "") + ":repair"})
            err = _json_problem(res.content, required)
        if err:
            raise LLMError(f"chat_json: {err}")
        return json.loads(res.content)


def _json_problem(content: str | None, required: list[str] | None) -> str | None:
    try:
        data = json.loads(content or "")
    except json.JSONDecodeError as e:
        return f"invalid JSON: {e.msg}"
    if not isinstance(data, dict):
        return "top-level value is not an object"
    missing = [k for k in (required or []) if k not in data]
    return f"missing keys {missing}" if missing else None
