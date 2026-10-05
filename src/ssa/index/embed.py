"""Embed all chunks once (content-hash cache) and write an aligned matrix.

Run:  python -m ssa.index.embed --dry-run      # cost estimate only, no API call
      python -m ssa.index.embed --limit 50     # smoke test (spends < $0.001)
      python -m ssa.index.embed                # full corpus (~1.2M tokens)

Outputs
  data/index/embedding_cache.sqlite      sha256(model + text) -> float32 vector
  data/index/embeddings.<model>.npy      L2-normalised, row i = chunks rowid i+1
  data/index/embed_log.json              counts, tokens, cost of this build

Re-running after a chunking change only pays for chunks whose text changed.
"""
from __future__ import annotations

import argparse
import hashlib
import logging
import sqlite3
import sys

import numpy as np

from ..data import config as C
from ..data.io_utils import read_jsonl, utcnow, write_json
from ..llm.client import LLMClient, price, spent_usd

log = logging.getLogger("embed")
BATCH_CHARS = 120_000      # ~30k tokens per request
BATCH_MAX = 96


def _h(model: str, text: str) -> str:
    return hashlib.sha256(f"{model}\n{text}".encode()).hexdigest()


def cache() -> sqlite3.Connection:
    con = sqlite3.connect(C.EMB_CACHE)
    con.execute("CREATE TABLE IF NOT EXISTS emb(h TEXT PRIMARY KEY, model TEXT, dim INT, vec BLOB)")
    return con


def matrix_path(model: str):
    return C.INDEX / f"embeddings.{model}.npy"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, help="embed only the first N missing chunks")
    ap.add_argument("--model", help="override EMBED_MODEL")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    C.load_dotenv()
    import os
    model = args.model or os.environ.get("EMBED_MODEL", "text-embedding-3-small")
    chunks = read_jsonl(C.CHUNKS)
    con = cache()
    have = {h for (h,) in con.execute("SELECT h FROM emb WHERE model=?", (model,))}
    todo = [(i, c) for i, c in enumerate(chunks) if _h(model, c["embed_text"]) not in have]
    chars = sum(len(c["embed_text"]) for _, c in todo)
    est = chars / 4 / 1e6 * price(model)[0]
    log.info("model=%s chunks=%d cached=%d to_embed=%d (~%d tokens, ~$%.4f); ledger so far $%.4f",
             model, len(chunks), len(chunks) - len(todo), len(todo), chars // 4, est, spent_usd())
    if args.dry_run:
        return 0
    if args.limit:
        todo = todo[: args.limit]

    if todo:
        llm = LLMClient()
        batch: list = []
        size = done = 0

        def flush():
            nonlocal batch, size, done
            if not batch:
                return
            vecs = llm.embed([c["embed_text"] for _, c in batch], model=model, tag="index")
            con.executemany("INSERT OR REPLACE INTO emb VALUES (?,?,?,?)",
                            [(_h(model, c["embed_text"]), model, len(v),
                              np.asarray(v, dtype=np.float32).tobytes())
                             for (_, c), v in zip(batch, vecs)])
            con.commit()                      # each batch is durable -> resumable
            done += len(batch)
            log.info("embedded %d/%d", done, len(todo))
            batch, size = [], 0

        for item in todo:
            n = len(item[1]["embed_text"])
            if batch and (size + n > BATCH_CHARS or len(batch) >= BATCH_MAX):
                flush()
            batch.append(item)
            size += n
        flush()

    # assemble matrix aligned with chunks.jsonl order (== sqlite rowid - 1)
    rows = {h: (d, v) for h, d, v in con.execute("SELECT h, dim, vec FROM emb WHERE model=?", (model,))}
    missing = [i for i, c in enumerate(chunks) if _h(model, c["embed_text"]) not in rows]
    if missing:
        log.warning("%d chunks still without embeddings (use without --limit); matrix not written",
                    len(missing))
        return 2
    M = np.stack([np.frombuffer(rows[_h(model, c["embed_text"])][1], dtype=np.float32)
                  for c in chunks])
    M /= np.linalg.norm(M, axis=1, keepdims=True) + 1e-12
    np.save(matrix_path(model), M)
    write_json(C.INDEX / "embed_log.json", {"built_at": utcnow(), "model": model,
                                           "chunks": len(chunks), "dim": int(M.shape[1]),
                                           "ledger_total_usd": round(spent_usd(), 6)})
    log.info("wrote %s %s", matrix_path(model).name, M.shape)
    return 0


if __name__ == "__main__":
    sys.exit(main())
