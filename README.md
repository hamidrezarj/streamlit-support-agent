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

## 5. Problem 1 — baseline and scoring

```bash
PYTHONPATH=src python -m ssa.eval.p1 --system baseline --split dev        # ~16 calls, ~$0.015
LLM_CACHE_MODE=replay PYTHONPATH=src python -m ssa.eval.p1 --system baseline --split dev   # $0 re-run
```

**Baseline** (`ssa.agent.baseline`): title + body → flat hybrid top-10 (same leakage filters)
→ one `gpt-4o-mini` call → the shared output contract (`ssa.agent.schema`). It is the
reference point the final pipeline must beat on the same cases, model and budget caps.

**Output contract** (baseline and final): `outcome` (usage_guidance / known_issue /
info_request / escalate), `known_facts`, `hypotheses` with a `basis`
(documented / similar_report / assistant_hypothesis) and sources, `unknowns`,
`next_step`, `questions`, `user_reply` with inline `[S#]` citations, `maintainer_summary`.
Citations are resolved to source ids **in code**; citations to unknown sources are counted.

**Scoring** (`ssa.eval.p1`, judge-free): outcome accuracy (exact and right-kind per bucket,
with a confusion table so "always ask" is not a win), retrieval recall and cited recall on
labelled sources, invalid citations, redundant questions (asks for a field the report already
gives), and cost (live vs replayed calls, tokens, USD, latency, steps). Systems only receive
`visible` + `filters`; gold is stripped by the runner. Results: `data/eval/runs/`.
The test split refuses to run without `--confirm-test`.
`--rescore` re-scores a saved run against the current gold without any API call.

**Three systems on the same cases** (`--system baseline | baseline-v2 | final`):

| System | Retrieval | LLM calls | What it adds |
|---|---|---|---|
| `baseline` | 1 query, flat hybrid top-10 | 1 | — |
| `baseline-v2` | same | 1 | the shared `agent/rules.py` text in the prompt (isolates *wording*) |
| `final` (`agent/pipeline.py`) | report + 3 LLM queries + error signatures, grouped (docs / release notes / issues), RRF-fused, 1 chunk per source, ~14 sources | 3 | per-source assessment, hypotheses with a discriminating check, decision rules **in code**, enforcement in code |

Final pipeline, enforced in code (not by the prompt):
* `known_issue` needs a same-cause release note **newer than the user's version** or a maintainer
  statement; `usage_guidance` needs same-cause docs/config — otherwise downgraded to
  `info_request` (if a discriminating check exists) or `escalate`.
* Upgrade advice only with a fixing release note; `confidence` from the evidence, not the model.
* `basis` of each hypothesis set from its sources' types; citations to unknown or irrelevant
  sources removed; questions for fields the report already gives (or ≥3-field checklists) dropped, max 2.
* Env values the LLM extracts are kept only if the quoted text is in the report.
* Every step is in the run file's `trace` (facts, queries, assessments, decision rules, fixes).

Extra metrics (all systems): basis mislabel rate, checklist questions, duplicate citations,
upgrade advice without a release note, high-confidence wrong answers, sources shown.


## 6. Command-line interface

```bash
export PYTHONPATH=src
python -m ssa.cli cases                                   # list dev cases (--split test|all)
python -m ssa.cli show    --case c-8149 --gold            # pretty-print a saved run, $0, no API
python -m ssa.cli show    --case c-12709 --run baseline --gold   # same case, baseline output
python -m ssa.cli analyze --case c-5500                   # run final-v4 now (cached calls replay, $0)
python -m ssa.cli analyze --title "App stuck on Please wait" --body-file report.txt --as-of 2024-05-01
pbpaste | python -m ssa.cli analyze --title "..." --body-file -       # report from the clipboard
```

Sections printed: facts from the report, evidence (relation per source, `RESOLVES`, maintainer reply,
`*` = cited), decision (rule that fired, LLM proposal, overrides), hypotheses with basis, next step and
questions, reply to the user, maintainer summary, what the code corrected, cost (live vs replayed).
`--gold` shows the hidden gold *after* the result (never sent to the system); `--all-sources` also lists
sources judged irrelevant; `--json` / `--save` give the full result with trace. A new report is
retrieved with the same leakage filters (`before = --as-of`, all eval issues excluded). Test cases
need `--confirm-test`.

## 7. Problem 2 — following a case (multi-turn agent with human approval)

Architecture: `docs/architecture_p2.png`. Deterministic orchestrator with LLM steps; every
visible change goes through the maintainer gate.

| Piece | Module |
|---|---|
| Local tracker (SQLite, resettable, idempotent keyed writes, fault injection) | `ssa.tracker.store` |
| Case memory (facts + supersession history, tried checks, rejection notes, proposals) | `ssa.agent.state` |
| Typed tools + `MaintainerGate` (the only path to approve / edit / reject / write) | `ssa.agent.tools` |
| Orchestrator (memory update → final-v4 analysis → multi-turn rules → proposal) | `ssa.agent.orchestrator` |
| Audit log (JSONL, secrets masked) | `ssa.agent.audit` |
| Scenario runner + code checks on stored state | `ssa.eval.p2` |

```bash
export PYTHONPATH=src
python -m ssa.cli tracker tools                    # tool names, inputs, outputs, errors (GATED marked)
python -m ssa.cli case open    --case c-5500
python -m ssa.cli case turn    --case c-5500        # proposes; nothing is visible yet
python -m ssa.cli case approve --case c-5500 --proposal p-... --hash <first 12 chars shown>
python -m ssa.cli case say     --case c-5500 --text "It's behind our nginx under /st/"
python -m ssa.cli case turn    --case c-5500
python -m ssa.cli case status  --case c-5500        # tracker + memory (facts, superseded, tried) + proposals
python -m ssa.cli case reject  --case c-5500 --proposal p-... --hash ... --reason "ask for logs first"
python -m ssa.cli tracker reset                    # empty tracker + case memory
python -m ssa.eval.p2 --system final    --split dev     # replay the multi-turn scenarios
python -m ssa.eval.p2 --system baseline --split dev     # memoryless agent + naive executor
```

Guarantees enforced in code (tests in `tests/test_p2_safety.py`, `tests/test_p2_agent.py`):
approval bound to case + content hash + ticket version (stale → refused); edit = new proposal,
posted verbatim; reject = nothing written, reason kept for the next draft; every write keyed
`<proposal>:<i>` (retries and duplicate deliveries never post twice); results read back and
verified, failures never reported as success; `closed_resolved` only after a quoted user
confirmation; facts kept only with a quote from the messages; corrections supersede (history
kept); step limits 8 per turn / 30 per case (limit → escalation proposal); issue / comment /
document text can never approve or act. Runtime state lives in `data/tracker/`, `data/state/`,
`data/audit/` (git-ignored); scenario runs keep `audit.jsonl` per scenario as sample traces.


## 8. Demo UI (Streamlit)

```bash
pip install -r requirements.txt                       # includes streamlit
PYTHONPATH=src streamlit run app/ui.py                # live: eval cases replay from the LLM cache ($0)
SSA_UI_FAKE=1 PYTHONPATH=src streamlit run app/ui.py  # offline demo: scripted LLM, no API key, $0
```

Three tabs: **Analyse a report** (Problem 1: eval case or new report, final vs baseline side by side,
facts, evidence with relation / resolves / cited, decision with the rule that fired, reply, gold after the
result), **Follow a case** (Problem 2: user messages, agent turns, the pending proposal with approve / edit /
reject, the tracker thread, case memory with superseded values, proposals and audit log, plus *simulate*
buttons: restart the process, deliver the last approval again, fail the next post after writing) and
**Results** (saved P1 / P2 runs). The UI has its own tracker and memory in `data/ui/` (git-ignored) and
never touches evaluation runs; only dev cases are offered. Logic lives in `ssa.ui.backend`; click-through
tests in `tests/test_ui.py` (Streamlit `AppTest`, offline).
