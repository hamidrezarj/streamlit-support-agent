# Streamlit Support Agent — QBC13 Agentic AI final project

Domain: **running & deploying Streamlit apps** (server, proxies, WebSockets, Docker,
config, Community Cloud).

## 1. Data pipeline

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt   # dev = runtime + pytest
cp .env.example .env          # put a read-only GitHub token in GITHUB_TOKEN
./scripts/build_data.sh       # docs -> issues -> clean -> manifest (resumable)
PYTHONPATH=src python -m pytest -q tests
```

| Step | Module | Output |
|---|---|---|
| Docs snapshot | `ssa.data.crawl_docs` | `docs_sections.jsonl`, `config_options.jsonl`, `release_notes.jsonl`, `versions.json` |
| Issue crawl | `ssa.data.crawl_issues` | `data/raw/github/{issues,comments,timeline}/N.json`, `crawl_log.json` |
| Cleaning + linking | `ssa.data.clean_issues` | `issues.jsonl`, `issue_families.json`, `cleaning_report.json` |
| Manifest | `ssa.data.manifest` | `data/manifest.json`, `data/selected_issues.json` |

### Reproducibility
* Docs are pinned to a commit in `data/raw/docs.lock` (`--update` moves the pin).
  `<Autofunction>` pages are rendered from the docs repo's `python/streamlit.json`
  (newest Streamlit version in that file, recorded in the manifest).
* GitHub data is fetched **once**; every raw file stores `fetched_at`. Re-runs skip
  cached files. Evaluation never reads from the web.
* Sampling: union of domain-label listing and keyword search (state=all), PRs
  dropped (`pull_request` field), created ≥ 2021-01-01, ordered by
  (has label, #keyword hits, #comments), capped at 600. All in `config.py`.

### Cleaning decisions
* Removed: bot comments, "+1 / any update" noise, issue-template boilerplate,
  images, @mentions, logins, e-mails, home-directory user names. Counts in
  `cleaning_report.json`.
* Env fields (Streamlit/Python version, OS, browser) are parsed from the
  "Debug info" block. Missing or placeholder values stay `null`.
* `labels`, `state`, `state_reason` are hints, not ground truth; label history
  is kept from the timeline.
* `fixed_in` (from release notes) and later comments are **future information**
  for a historical case — the eval/index builder must filter by date.
* `family_id` groups duplicates ("Duplicate of #N"); the eval split is done
  per family to avoid leakage.

Secrets: the token is read from `.env`/env only, never logged or written.

## 2. Search index

```bash
# fill LLM_BASE_URL / LLM_API_KEY / EMBED_MODEL in .env first
./scripts/build_index.sh
PYTHONPATH=src python -m ssa.index.search "stuck on Please wait behind nginx" --before 2024-01-01T00:00:00Z
```

| Step | Module | Output |
|---|---|---|
| Chunking | `ssa.index.chunk` | `data/index/chunks.jsonl` (5,990 chunks), `chunk_stats.json` |
| BM25 | `ssa.index.store` | `data/index/index.sqlite` (chunks table + FTS5, porter tokenizer) |
| Embeddings | `ssa.index.embed` | `embedding_cache.sqlite` (content-hash cache), `embeddings.<model>.npy` |
| Retrieval | `ssa.index.search` | `Retriever.search()` / `search_grouped()` — bm25, dense, hybrid (RRF k=60) |
| Sanity eval | `ssa.eval.retrieval_auto` | `data/eval/retrieval_auto.json` |

**Chunk types:** issue body (≤2,400 chars), comment windows (role + date per
comment), docs section, config option, release-note bullet. Each chunk has
`available_at`, `issue_number`, `family_id` for leakage filters. Labels, state
and `fixed_in` are *not* in chunk text.

**Leakage filters** (`Filters`): `before` (drop text that did not exist yet),
`exclude_issues` / `exclude_families` (eval cases + duplicates), and release
notes that link an excluded issue. Docs are a single snapshot (pinned commit),
so they are treated as timeless — a known limitation for "historical" cases.

**Cost:** the corpus is ~1.15M tokens ≈ $0.02 with text-embedding-3-small.
Every API call is logged in `data/usage/ledger.jsonl`; `BUDGET_USD` blocks a
call *before* it is sent if it would exceed the team budget.

## 3. Evaluation set

```bash
PYTHONPATH=src python -m ssa.eval.select_cases          # reviewable candidate pool (heuristic bucket hints)
PYTHONPATH=src python -m ssa.eval.build_cases           # cases + scenarios + validation report
PYTHONPATH=src python -m ssa.eval.retrieval_gold --split dev
```

* **32 cases** (16 dev / 16 test), hand-picked from 139 candidates; buckets
  answerable / ambiguous / escalate are balanced per split. **11 multi-turn
  scenarios** (5 dev / 6 test) covering user correction, maintainer edit/reject,
  stop-while-awaiting-approval, duplicate request, case changed before execute,
  tool failure, switching between two cases.
* **Historical setting:** a case starts at the issue's `created_at`; the assistant
  sees only title + body. Retrieval for every case uses `before=as_of` and
  excludes **all** eval issues (`data/eval/eval_exclusions.json`).
* **Split by topic** (near-duplicate group) and duplicate family — no topic
  appears in both dev and test. Max cross-split body similarity is 0.75.
* Real follow-ups are copied from the issue author's comments (`origin: real`);
  our own turns are marked `origin: designed`.
* Annotations in `data/eval/annotations/*_draft.json` were drafted by Claude
  from the full threads and **must be reviewed by the team** (`reviewed_by`).
* Release-note ids are content hashes (`rn:<version>:<sha1[:8]>`), stable across re-parsing.
* Known limitations: docs are a 2026 snapshot (treated as timeless); patch
  releases (e.g. 1.45.1) are not in the release notes, so such fixes are only
  evidenced by maintainer comments; issue bodies may have been edited after creation.

## 4. LLM access: chat, cost ledger, record/replay

```bash
PYTHONPATH=src python -m ssa.llm.smoke                      # one tiny live call (~$0.00002)
LLM_CACHE_MODE=replay PYTHONPATH=src python -m ...          # re-run anything offline, $0
```

| Piece | Module | Notes |
|---|---|---|
| `LLMClient.chat()` | `ssa.llm.client` | OpenAI-compatible (Metis); tools / tool_choice / JSON mode; `temperature=0`, `seed=7` |
| `LLMClient.chat_json()` | `ssa.llm.client` | JSON mode + one repair turn on invalid JSON / missing keys, then `LLMError` |
| Record/replay cache | `ssa.llm.cache` | `data/llm_cache/chat.sqlite`, key = sha256 of the full request; committed to git |
| Fake client | `ssa.llm.fake` | scripted responses / tool calls for offline logic tests |

`LLM_CACHE_MODE`: `cache` (default, read-through) · `replay` (offline; a miss raises
`ReplayMiss`; no API key needed) · `live` (always call, overwrite) · `off`.

* **Live vs replayed are never mixed:** live calls are budget-guarded and written to
  `data/usage/ledger.jsonl`; replayed calls cost $0, are not ledgered, and are flagged
  `replayed=True`. `client.stats` counts both per run (`live_calls`, `replayed_calls`,
  tokens, `usd_live`, `usd_replayed` = original cost of the replayed calls, latency).
* Ledger prices are OpenAI list prices (override with `PRICE_<MODEL>_IN/_OUT`); the
  Metis panel is the source of truth for actual spend.
* Default chat model: `gpt-4o-mini`; agent replies are in **English** (fixed for evaluation).
