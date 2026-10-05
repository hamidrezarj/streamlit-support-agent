"""OpenAI-compatible client for the Metis gateway, with a cost ledger + budget guard.

* Config from .env: LLM_BASE_URL, LLM_API_KEY, EMBED_MODEL, CHAT_MODEL, BUDGET_USD.
* Every real call appends one line to data/usage/ledger.jsonl
  (time, kind, model, tokens, estimated USD) - this is the cost report source.
* Before a call, the guard estimates its cost; if ledger + estimate > budget,
  BudgetExceeded is raised and nothing is sent (state stays intact).
* The API key is only placed in the Authorization header; never logged.
"""
from __future__ import annotations

import json
import logging
import os
import time

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
        if not self.base or not self._key:
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
