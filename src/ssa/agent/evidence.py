"""Turn retrieval hits into the numbered evidence block [S1]..[Sn] shown to the model."""
from __future__ import annotations

SNIPPET_CHARS = 1200   # per source; keeps a 10-source prompt around 3-4k tokens


def label_hits(hits) -> dict[str, dict]:
    """S-label -> source metadata (what citations resolve to)."""
    return {f"S{i}": {"source_id": h.source_id, "type": h.type, "title": h.title,
                      "heading": h.heading, "url": h.url, "available_at": h.available_at,
                      "chunk_id": h.chunk_id, "rank": i}
            for i, h in enumerate(hits, 1)}


def format_hits(hits, snippet_chars: int = SNIPPET_CHARS) -> str:
    out = []
    for i, h in enumerate(hits, 1):
        date = (h.available_at or "docs snapshot")[:10]
        text = h.text if len(h.text) <= snippet_chars else h.text[:snippet_chars] + " ...[cut]"
        out.append(f"[S{i}] {h.type} | {h.title} | {h.heading} | {date} | {h.url}\n{text}")
    return "\n\n".join(out)
