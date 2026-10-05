"""Hybrid retriever: BM25 (FTS5) + dense (embeddings) fused with RRF.

    from ssa.index.search import Retriever, Filters
    r = Retriever()                                   # mode="hybrid" if matrix exists
    hits = r.search("stuck on Please wait behind nginx",
                    Filters(before="2024-05-01T00:00:00Z", exclude_families={"fam:14290"}))

Leakage filters (applied before ranking is cut, so k results are always clean):
  before            drop chunks whose available_at >= before (timeless docs kept
                    unless include_timeless=False)
  exclude_issues    drop all chunks of these issues ...
  exclude_families  ... and of every issue in these duplicate families
  release notes that link an excluded issue are dropped too (they would state
  the answer for that very case)
Diversity: at most `max_per_source` chunks per source (issue / doc page section).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass, field

import numpy as np

from ..data import config as C
from .store import connect, fts_query

log = logging.getLogger("search")
RRF_K = 60


@dataclass
class Filters:
    before: str | None = None
    include_timeless: bool = True
    exclude_issues: set[int] = field(default_factory=set)
    exclude_families: set[str] = field(default_factory=set)
    types: set[str] | None = None            # e.g. {"doc", "release_note"}
    drop_linked_release_notes: bool = True   # False only for auto-labelled retrieval eval


@dataclass
class Hit:
    chunk_id: str
    source_id: str
    type: str
    title: str
    heading: str
    url: str
    text: str
    available_at: str | None
    score: float
    ranks: dict

    def cite(self) -> str:
        return f"{self.type}:{self.source_id} ({self.url})"


class Retriever:
    def __init__(self, mode: str | None = None, embed_model: str | None = None):
        C.load_dotenv()
        self.con = connect()
        rows = self.con.execute("SELECT rowid, chunk_id, source_id, type, title, heading, url, text, "
                                "available_at, issue_number, family_id, meta FROM chunks ORDER BY rowid").fetchall()
        self.rows = rows
        self.meta = [json.loads(r["meta"] or "{}") for r in rows]
        self.embed_model = embed_model or os.environ.get("EMBED_MODEL", "text-embedding-3-small")
        mpath = C.INDEX / f"embeddings.{self.embed_model}.npy"
        self.M = np.load(mpath) if mpath.exists() else None
        if self.M is not None and self.M.shape[0] != len(rows):
            log.warning("embedding matrix out of date (%d vs %d chunks) - dense disabled",
                        self.M.shape[0], len(rows))
            self.M = None
        self.mode = mode or ("hybrid" if self.M is not None else "bm25")
        if self.mode != "bm25" and self.M is None:
            raise RuntimeError("dense/hybrid needs embeddings: run python -m ssa.index.embed")
        self._llm = None
        self.query_embeddings = 0

    # ---------------------------------------------------------------- filters
    def _allowed(self, i: int, f: Filters) -> bool:
        r = self.rows[i]
        if f.types and r["type"] not in f.types:
            return False
        if r["issue_number"] is not None and (r["issue_number"] in f.exclude_issues
                                              or r["family_id"] in f.exclude_families):
            return False
        if r["type"] == "release_note" and f.drop_linked_release_notes and f.exclude_issues and \
                set(self.meta[i].get("linked_issues", [])) & f.exclude_issues:
            return False
        av = r["available_at"]
        if f.before:
            if av is None:
                return f.include_timeless
            if av >= f.before:
                return False
        return True

    # ---------------------------------------------------------------- rankers
    def _bm25(self, query: str, f: Filters, n: int) -> list[int]:
        q = fts_query(query)
        if not q:
            return []
        out = []
        for (rowid,) in self.con.execute(
                "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ? "
                "ORDER BY bm25(chunks_fts, 2.0, 1.0, 1.0) LIMIT 400", (q,)):
            if self._allowed(rowid - 1, f):
                out.append(rowid - 1)
                if len(out) >= n:
                    break
        return out

    def _qvec(self, query: str) -> np.ndarray:
        from .embed import cache, _h
        con = cache()
        h = _h(self.embed_model, query)
        row = con.execute("SELECT vec FROM emb WHERE h=?", (h,)).fetchone()
        if row:
            v = np.frombuffer(row[0], dtype=np.float32)
        else:
            if self._llm is None:
                from ..llm.client import LLMClient
                self._llm = LLMClient()
            v = np.asarray(self._llm.embed([query], model=self.embed_model, tag="query")[0],
                           dtype=np.float32)
            con.execute("INSERT OR REPLACE INTO emb VALUES (?,?,?,?)",
                        (h, self.embed_model, len(v), v.tobytes()))
            con.commit()
            self.query_embeddings += 1
        return v / (np.linalg.norm(v) + 1e-12)

    def _dense(self, query: str, f: Filters, n: int) -> list[int]:
        sims = self.M @ self._qvec(query)
        out = []
        for i in np.argsort(-sims)[:2000]:
            if self._allowed(int(i), f):
                out.append(int(i))
                if len(out) >= n:
                    break
        return out

    # ---------------------------------------------------------------- public
    def search(self, query: str, filters: Filters | None = None, k: int = 10,
               max_per_source: int = 2, mode: str | None = None, pool: int = 100) -> list[Hit]:
        f = filters or Filters()
        mode = mode or self.mode
        ranks: dict[int, dict] = {}
        lists = []
        if mode in ("bm25", "hybrid"):
            lists.append(("bm25", self._bm25(query, f, pool)))
        if mode in ("dense", "hybrid"):
            lists.append(("dense", self._dense(query, f, pool)))
        for name, lst in lists:
            for rank, i in enumerate(lst, 1):
                ranks.setdefault(i, {})[name] = rank
        scored = sorted(ranks.items(), key=lambda kv: -sum(1 / (RRF_K + r) for r in kv[1].values()))
        hits, per_src = [], {}
        for i, rk in scored:
            r = self.rows[i]
            if per_src.get(r["source_id"], 0) >= max_per_source:
                continue
            per_src[r["source_id"]] = per_src.get(r["source_id"], 0) + 1
            hits.append(Hit(r["chunk_id"], r["source_id"], r["type"], r["title"], r["heading"],
                            r["url"], r["text"], r["available_at"],
                            round(sum(1 / (RRF_K + x) for x in rk.values()), 5), rk))
            if len(hits) >= k:
                break
        return hits

    GROUPS = {"issues": {"issue", "comment"}, "docs": {"doc", "config_option"},
              "release_notes": {"release_note"}}

    def search_grouped(self, query: str, filters: Filters | None = None,
                       per_group: dict | None = None, mode: str | None = None) -> dict[str, list[Hit]]:
        """Separate top-k per evidence kind, so docs and release notes are not
        crowded out by the (more numerous, more verbose) issues."""
        per_group = per_group or {"issues": 5, "docs": 4, "release_notes": 3}
        base = filters or Filters()
        out = {}
        for g, k in per_group.items():
            f = Filters(**{**base.__dict__, "types": self.GROUPS[g]})
            out[g] = self.search(query, f, k=k, mode=mode)
        return out


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="ad-hoc search from the command line")
    ap.add_argument("query")
    ap.add_argument("-k", type=int, default=8)
    ap.add_argument("--mode", choices=["bm25", "dense", "hybrid"])
    ap.add_argument("--before")
    a = ap.parse_args()
    r = Retriever()
    for h in r.search(a.query, Filters(before=a.before), k=a.k, mode=a.mode):
        print(f"{h.score:.4f} {h.ranks} [{h.type}] {h.title[:70]} | {h.heading[:40]}\n    {h.url}")


if __name__ == "__main__":
    main()
