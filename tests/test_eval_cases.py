"""Integrity of the evaluation set (skipped if the case files were not built)."""
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "data" / "eval"
CASES = sorted((ROOT / "cases").glob("*/*.json"))
pytestmark = pytest.mark.skipif(not CASES, reason="run python -m ssa.eval.build_cases first")


def _load():
    return [json.loads(p.read_text()) for p in CASES]


def test_split_sizes_and_disjoint_topics():
    cs = _load()
    assert len(cs) >= 30
    for key in ("topic", "family_id"):
        splits = {}
        for c in cs:
            splits.setdefault(c[key], set()).add(c["split"])
        assert all(len(v) == 1 for v in splits.values()), key


def test_visible_input_has_no_hidden_fields():
    for c in _load():
        assert set(c["visible"]) == {"issue_number", "url", "title", "body", "as_of"}
        assert c["filters"]["before"] == c["visible"]["as_of"]
        assert c["visible"]["issue_number"] in c["filters"]["exclude_issues"]


def test_scenarios_mark_turn_origin():
    sc = [json.loads(p.read_text()) for p in sorted((ROOT / "scenarios").glob("*/*.json"))]
    assert len(sc) >= 10 and sum(s["split"] == "test" for s in sc) >= len(sc) / 2
    for s in sc:
        for st in s["steps"]:
            if st["type"] == "user":
                assert st["origin"] in ("real", "designed") and st.get("text")
