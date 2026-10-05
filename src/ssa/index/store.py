"""SQLite store: chunk table + FTS5 (BM25) index.

Run:  python -m ssa.index.store       -> data/index/index.sqlite (rebuilt from chunks.jsonl)

FTS5 with the porter/unicode61 tokenizer splits `server.baseUrlPath` into
`server` + `baseurlpath` and `STREAMLIT_SERVER_PORT` into one token, which
works well for config names and error messages.
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys

from ..data import config as C
from ..data.io_utils import read_jsonl

SCHEMA = """
CREATE TABLE chunks (
  rowid INTEGER PRIMARY KEY,
  chunk_id TEXT UNIQUE NOT NULL,
  source_id TEXT NOT NULL,
  type TEXT NOT NULL,
  title TEXT, heading TEXT, url TEXT,
  text TEXT NOT NULL,
  available_at TEXT,          -- NULL = timeless snapshot (docs/config)
  issue_number INTEGER,
  family_id TEXT,
  version TEXT,
  meta TEXT                   -- remaining fields as JSON
);
CREATE INDEX ix_chunks_type ON chunks(type);
CREATE INDEX ix_chunks_issue ON chunks(issue_number);
CREATE VIRTUAL TABLE chunks_fts USING fts5(
  title, heading, text, content='chunks', content_rowid='rowid',
  tokenize='porter unicode61'
);
"""
CORE = {"chunk_id", "source_id", "type", "title", "heading", "url", "text",
        "available_at", "issue_number", "family_id", "version", "embed_text"}


def build() -> int:
    C.INDEX_DB.unlink(missing_ok=True)
    con = sqlite3.connect(C.INDEX_DB)
    con.executescript(SCHEMA)
    rows = read_jsonl(C.CHUNKS)
    con.executemany(
        "INSERT INTO chunks(rowid,chunk_id,source_id,type,title,heading,url,text,available_at,"
        "issue_number,family_id,version,meta) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(i + 1, r["chunk_id"], r["source_id"], r["type"], r["title"], r["heading"], r["url"],
          r["text"], r.get("available_at"), r.get("issue_number"), r.get("family_id"),
          r.get("version"), json.dumps({k: v for k, v in r.items() if k not in CORE}))
         for i, r in enumerate(rows)])
    con.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('rebuild')")
    con.commit()
    con.close()
    return len(rows)


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{C.INDEX_DB}?mode=ro", uri=True, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con


_TOKEN = re.compile(r"[A-Za-z0-9_]{2,}")
STOP = set("""a an and are as at be but by for from has have i if in into is it its of on or
so that the their then there these this to was were will with my me we you your our can
not no do does did how what when why which who after before using use used get got""".split())


def fts_query(text: str, max_terms: int = 40) -> str:
    """Free text -> safe FTS5 OR-query of quoted tokens (no syntax injection)."""
    seen, terms = set(), []
    for t in _TOKEN.findall(text.lower()):
        if t in STOP or t in seen:
            continue
        seen.add(t)
        terms.append(f'"{t}"')
        if len(terms) >= max_terms:
            break
    return " OR ".join(terms)


if __name__ == "__main__":
    print("indexed chunks:", build())
    sys.exit(0)
