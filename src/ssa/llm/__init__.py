"""LLM access. Use get_llm() so the cache mode / fake client can be swapped in one place."""
from __future__ import annotations


def get_llm():
    from .client import LLMClient
    return LLMClient()
