"""Turn processed records into retrieval chunks.

Run:  python -m ssa.index.chunk        -> data/index/chunks.jsonl

One chunk = one citable piece of evidence. Every chunk carries:
  chunk_id, source_id, type, title, heading, url, text, embed_text,
  available_at  - earliest time this text existed (for "no future info" filters)
  issue_number, family_id - so eval issues + their duplicates can be excluded
  version, release_date   - for release notes

Chunk types
  issue          title + body of an issue (split if long). available_at = created_at
  comment        a window of consecutive comments.  available_at = last comment time
  doc            docs section (split if long). available_at = None (snapshot, see README)
  config_option  one config.toml option
  release_note   one release-note bullet. available_at = release date

What is deliberately NOT in chunk text: labels, state, fixed_in. Those are
metadata a case may not know yet; the agent gets them only via explicit tools.
"""
from __future__ import annotations

import hashlib
import logging
import re
import sys
from collections import Counter

from ..data import config as C
from ..data.io_utils import read_jsonl, write_json, write_jsonl, utcnow

log = logging.getLogger("chunk")


def split_text(text: str, max_chars: int = C.CHUNK_MAX_CHARS,
               overlap: int = C.CHUNK_OVERLAP_PARAS) -> list[str]:
    """Split on blank lines, keeping ``` fences whole when possible."""
    if len(text) <= max_chars:
        return [text]
    # paragraphs, but never split inside a code fence
    paras, buf, in_code = [], [], False
    for line in text.split("\n"):
        if line.lstrip().startswith("```"):
            in_code = not in_code
        if not line.strip() and not in_code and buf:
            paras.append("\n".join(buf))
            buf = []
        else:
            buf.append(line)
    if buf:
        paras.append("\n".join(buf))
    # hard-split oversize paragraphs by lines
    flat: list[str] = []
    for p in paras:
        while len(p) > max_chars:
            cut = p.rfind("\n", 0, max_chars)
            cut = cut if cut > max_chars // 3 else max_chars
            flat.append(p[:cut])
            p = p[cut:].lstrip("\n")
        if p.strip():
            flat.append(p)
    chunks, cur = [], []
    for p in flat:
        if cur and sum(len(x) + 2 for x in cur) + len(p) > max_chars:
            chunks.append("\n\n".join(cur))
            cur = cur[-overlap:] if overlap and len(cur[-1]) < max_chars // 2 else []
        cur.append(p)
    if cur:
        chunks.append("\n\n".join(cur))
    return chunks


def _cid(*parts) -> str:
    return hashlib.sha1("|".join(map(str, parts)).encode()).hexdigest()[:16]


def _row(source_id, typ, title, heading, url, text, embed_prefix, **meta) -> dict:
    return {"chunk_id": _cid(source_id, heading, text[:200]), "source_id": source_id,
            "type": typ, "title": title, "heading": heading, "url": url, "text": text,
            "embed_text": (embed_prefix + "\n" + text)[: C.EMBED_MAX_CHARS], **meta}


# ----------------------------------------------------------------- issues
def issue_chunks(r: dict) -> list[dict]:
    out = []
    base = {"issue_number": r["number"], "family_id": r["family_id"]}
    prefix = f"GitHub issue #{r['number']}: {r['title']}"
    for i, part in enumerate(split_text(r["body"] or "(no description)")):
        out.append(_row(r["id"], "issue", r["title"], f"body part {i + 1}", r["url"],
                        part, prefix, available_at=r["created_at"], **base))
    # comment windows
    window, size = [], 0

    def flush():
        if not window:
            return
        txt = "\n\n".join(w[1] for w in window)
        first, last = window[0][0], window[-1][0]
        out.append(_row(r["id"], "comment", r["title"],
                        f"comments {first['created_at'][:10]}..{last['created_at'][:10]}",
                        first["url"], txt, prefix + " (discussion)",
                        available_at=last["created_at"],
                        has_maintainer=any(w[0]["is_maintainer"] for w in window),
                        comment_ids=[w[0]["id"] for w in window], **base))

    for c in r["comments"]:
        who = "maintainer" if c["is_maintainer"] else ("issue author" if c["is_issue_author"] else "user")
        block = f"[{who}, {c['created_at'][:10]}]\n{c['text']}"
        for piece in split_text(block):
            if size and size + len(piece) > C.CHUNK_MAX_CHARS:
                flush()
                window, size = [], 0
            window.append((c, piece))
            size += len(piece)
    flush()
    return out


# ----------------------------------------------------------------- docs etc.
def doc_chunks(d: dict) -> list[dict]:
    heading = " > ".join(d["heading_path"]) or d["title"]
    prefix = f"Streamlit docs: {d['title']} > {heading}"
    return [_row(d["id"], "doc", d["title"], heading + (f" ({i + 1})" if i else ""), d["url"],
                 part, prefix, available_at=None, docs_commit=d["docs_commit"])
            for i, part in enumerate(split_text(d["text"]))]


def config_chunk(c: dict) -> dict:
    return _row(c["id"], "config_option", c["title"], "config.toml", c["url"], c["text"],
                "Streamlit config.toml option", available_at=None)


def release_chunk(r: dict) -> dict:
    return _row(r["id"], "release_note", r["title"], r["category"], r["url"], r["text"],
                f"Streamlit release notes, version {r['version']} ({r['release_date']}), {r['category']}:",
                available_at=(r["release_date"] + "T00:00:00Z") if r["release_date"] else None,
                version=r["version"], release_date=r["release_date"],
                linked_issues=r["linked_issues"], linked_prs=r["linked_prs"])


def build() -> dict:
    P = C.PROCESSED
    rows: list[dict] = []
    for r in read_jsonl(P / "issues.jsonl"):
        rows += issue_chunks(r)
    for d in read_jsonl(P / "docs_sections.jsonl"):
        rows += doc_chunks(d)
    rows += [config_chunk(c) for c in read_jsonl(P / "config_options.jsonl")]
    rows += [release_chunk(r) for r in read_jsonl(P / "release_notes.jsonl")]

    ids = Counter(r["chunk_id"] for r in rows)
    dups = {k for k, v in ids.items() if v > 1}
    if dups:  # identical text under the same source -> keep first
        seen, uniq = set(), []
        for r in rows:
            if r["chunk_id"] in seen:
                continue
            seen.add(r["chunk_id"])
            uniq.append(r)
        rows = uniq
    write_jsonl(C.CHUNKS, rows)
    by_type = Counter(r["type"] for r in rows)
    chars = sum(len(r["embed_text"]) for r in rows)
    stats = {"built_at": utcnow(), "chunks": len(rows), "by_type": dict(by_type),
             "duplicate_ids_dropped": sum(ids[k] - 1 for k in dups),
             "embed_chars": chars, "approx_embed_tokens": chars // 4,
             "max_chars": C.CHUNK_MAX_CHARS}
    write_json(C.INDEX / "chunk_stats.json", stats)
    return stats


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    log.info("chunks: %s", build())
    return 0


if __name__ == "__main__":
    sys.exit(main())
