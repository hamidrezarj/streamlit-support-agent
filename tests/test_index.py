"""Offline tests for chunking, FTS query building and leakage filters."""
from ssa.index.chunk import split_text
from ssa.index.search import Filters, Retriever
from ssa.index.store import fts_query


def test_split_respects_limit_and_code_fences():
    text = "para one " * 50 + "\n\n```\n" + "\n".join(f"line {i}" for i in range(30)) + "\n```\n\n" + "tail " * 100
    parts = split_text(text, max_chars=400)
    assert all(len(p) <= 400 for p in parts)
    assert len(parts) > 2


def test_fts_query_is_injection_safe():
    q = fts_query('NEAR(") OR * column:text "Please wait" server.baseUrlPath')
    assert q and all(t.startswith('"') and t.endswith('"') for t in q.split(" OR "))
    assert '"baseurlpath"' in q


class _Fake(Retriever):
    """Retriever with in-memory rows, no DB."""
    def __init__(self, rows, meta):
        self.rows, self.meta = rows, meta


def _row(t, issue=None, fam=None, av=None):
    return {"type": t, "issue_number": issue, "family_id": fam, "available_at": av}


def test_leakage_filters():
    rows = [_row("issue", 10, "fam:10", "2024-01-01"),       # excluded issue
            _row("comment", 11, "fam:10", "2023-01-01"),     # same family -> excluded
            _row("release_note", av="2023-06-01"),           # links #10 -> excluded
            _row("issue", 12, "fam:12", "2025-01-01"),       # after cutoff
            _row("doc"),                                     # timeless -> kept
            _row("issue", 13, "fam:13", "2023-02-01")]       # kept
    meta = [{}, {}, {"linked_issues": [10]}, {}, {}, {}]
    r = _Fake(rows, meta)
    f = Filters(before="2024-06-01", exclude_issues={10}, exclude_families={"fam:10"})
    assert [i for i in range(6) if r._allowed(i, f)] == [4, 5]
    f2 = Filters(before="2024-06-01", include_timeless=False)
    assert not r._allowed(4, f2)
