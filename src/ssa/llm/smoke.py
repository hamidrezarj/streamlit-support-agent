"""One tiny live chat call to check the gateway, model name, price and ledger.

Run:  PYTHONPATH=src python -m ssa.llm.smoke        (~$0.00002)
"""
from __future__ import annotations

from .client import LLMClient, spent_usd


def main() -> None:
    c = LLMClient()
    r = c.chat_json([{"role": "system", "content": "Reply with JSON only."},
                     {"role": "user", "content": 'Return {"ok": true, "product": "<name of the Python '
                                                 'framework for data apps by Snowflake>"}'}],
                    required=["ok"], max_tokens=30, tag="smoke")
    print("reply:", r)
    print(f"model={c.chat_model} cache_mode={c.cache_mode} stats={c.stats}")
    print(f"ledger total ${spent_usd():.4f} of ${c.budget:.2f}")


if __name__ == "__main__":
    main()
