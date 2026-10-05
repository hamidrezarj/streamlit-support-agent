"""Central configuration for the data pipeline.

Everything that decides *which* data ends up in the snapshot lives here, so the
sampling method can be reported and reproduced exactly.
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
DATA = ROOT / "data"
RAW = DATA / "raw"
PROCESSED = DATA / "processed"
GITHUB_RAW = RAW / "github"
DOCS_REPO_DIR = RAW / "docs_repo"
DOCS_LOCK = RAW / "docs.lock"
MANIFEST = DATA / "manifest.json"

REPO = "streamlit/streamlit"
DOCS_REPO_URL = "https://github.com/streamlit/docs.git"
DOCS_SITE = "https://docs.streamlit.io"

# --- Domain: running & deploying Streamlit apps ---------------------------
# Labels are applied inconsistently (mostly to newer issues), so we combine
# label listing with keyword search.
DOMAIN_LABELS = [
    "area:deployment",
    "area:server",
    "area:network",
    "area:docker",
    "feature:community-cloud",
    "feature:config",
    "feature:cli",
]

# Each phrase becomes: repo:streamlit/streamlit is:issue in:title,body "<phrase>"
DOMAIN_KEYWORDS = [
    "websocket",
    "reverse proxy",
    "nginx",
    "docker",
    "kubernetes",
    "baseUrlPath",
    "XSRF",
    "enableCORS",
    "Please wait",
    "connection error",
    "server.port",
    "server.address",
    "_stcore",
    "health check",
    "load balancer",
    "SSL",
    "Azure",
    "AWS",
    "Heroku",
    "sticky session",
]

CREATED_SINCE = "2021-01-01"        # Streamlit 1.0 shipped Oct 2021
MAX_ISSUES = 600                    # hard cap after union/dedupe
MAX_SEARCH_RESULTS_PER_KEYWORD = 100

# Long bodies/comments are trimmed (head + tail kept: tracebacks end at the tail)
MAX_BODY_CHARS = 12_000
MAX_COMMENT_CHARS = 6_000
MAX_CODE_BLOCK_LINES = 60


def load_dotenv(path: Path = ROOT / ".env") -> None:
    """Tiny .env loader (no dependency). Existing env vars win."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

# --- Index ------------------------------------------------------------------
INDEX = DATA / "index"
CHUNKS = INDEX / "chunks.jsonl"
INDEX_DB = INDEX / "index.sqlite"
EMB_CACHE = INDEX / "embedding_cache.sqlite"   # content-hash -> vector (never re-pay)
CHUNK_MAX_CHARS = 2400       # ~600 tokens; one cited piece of evidence
CHUNK_OVERLAP_PARAS = 1
EMBED_MAX_CHARS = 6000       # text sent to the embedding model per chunk
