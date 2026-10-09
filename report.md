# Streamlit Support Agent — Report

## Problem 1 — Investigating a report and proposing a path

> **Status:** dev split only (16 cases). The test split (16 cases) has **not** been run; it will be run once,
> with the frozen system, for the final numbers. All numbers below come from the saved run files in
> `data/eval/runs/history/`, re-scored with the same scorer (`ssa.eval.p1 --rescore`) against the same gold.

### TL;DR

| | baseline-v1 | baseline-v2 | **final-v4 (chosen)** |
|---|---|---|---|
| What it is | 1 query → top-10 → 1 LLM call | same + shared rules in the prompt | 3 LLM calls, grouped multi-query retrieval, decisions in code |
| Outcome exact (16 cases) | 0.19 (3/16) | 0.31 (5/16) | **0.69 (11/16)** |
| Answerable / ambiguous / escalate exact | 0.33 / 0.20 / 0.00 | 0.17 / 0.80 / 0.00 | **0.50 / 1.00 / 0.60** |
| Retrieval recall (labelled sources shown) | 0.47 | 0.47 | **0.75** |
| Wrong "documented" labels on hypotheses | 81% | 13% | **0%** |
| Upgrade advice without evidence (cases) | 4 | 2 | **0** |
| High-confidence wrong answers | 4 | 6 | **0** |
| Cost per case | $0.00083 | $0.00086 | $0.00174 (2.1×) |
| LLM latency per case | 6.8 s | 6.1 s | 16.0 s |

Final-v4 is right on **every** case either baseline got right, plus 8 more (vs v1: 8 wins / 0 losses,
two-sided sign test p = 0.008; vs v2: 6 / 0, p = 0.031).

---

### 1. Setup (identical for every system)

* **Cases:** dev split, 16 cases — 6 *answerable* (gold: usage_guidance 4, known_issue 2), 5 *ambiguous*
  (gold: info_request), 5 *escalate*. Historical setting: the system sees only title + body as of the
  issue's creation time; retrieval uses `before = as_of` and excludes all 32 eval issues (0 leaks).
* **Model:** `gpt-4o-mini` via Metis, `temperature = 0`, `seed = 7`, JSON mode. Same index
  (5,990 chunks, BM25 + `text-embedding-3-small`, RRF), same output contract (`agent/schema.py`),
  same scorer. Systems receive `{visible, filters}` only — gold is stripped by the runner.
* **Metrics** (`ssa.eval.p1`, all computed in code, no LLM judge):

| Metric | Meaning |
|---|---|
| outcome exact | predicted outcome = gold outcome (usage_guidance / known_issue / info_request / escalate) |
| right kind (bucket_ok) | outcome is the right kind for the bucket; reported with a confusion table so "always answer" or "always ask" cannot look good |
| retrieval recall | share of labelled relevant sources among the sources shown to the model |
| cited recall | share of labelled relevant sources the reply actually cites |
| basis mislabel rate | hypotheses whose claimed basis (documented / similar_report / assistant_hypothesis) contradicts the types of the sources they cite |
| checklist / redundant questions | asks ≥3 environment fields at once / asks for a field the report already gives |
| unsupported upgrade | recommends upgrading without citing a release note or a maintainer statement |
| high-confidence wrong | `confidence = high` and outcome wrong |
| LLM-proposed exact | (final only) accuracy of the LLM's own proposed outcome, before the code decision |

### 2. Retrieval (shared component)

Dev retrieval eval (`ssa.eval.retrieval_gold`, query = title + body, 15 labelled cases):

| Mode | recall@10 | recall@12 | hit@10 | MRR |
|---|---|---|---|---|
| bm25 | 0.49 | 0.49 | 0.67 | 0.48 |
| dense | 0.49 | 0.55 | 0.73 | **0.58** |
| hybrid (flat) — *used by baselines* | 0.47 | 0.48 | 0.67 | 0.51 |
| grouped-bm25 | 0.51 | 0.54 | 0.67 | 0.42 |
| grouped-dense | **0.65** | **0.66** | 0.80 | 0.52 |
| grouped-hybrid — *used by final* | 0.64 | 0.65 | **0.87** | 0.46 |

* Flat retrieval lets long GitHub issues crowd out docs and release notes (flat dense found **0 of 7**
  labelled release notes). Grouped search reserves slots per evidence type (issues / docs+config /
  release notes).
* **Bug found and fixed** during development: BM25 applied the type filter *after* `LIMIT 400`, so
  grouped search returned **no docs and no release notes** for 4 of 16 dev cases. The filter now runs in
  SQL; grouped-bm25 recall@12 went 0.49 → 0.54.
* The final pipeline adds multi-query retrieval (report + 3 LLM-written queries + error signatures,
  RRF-fused, one chunk per source, 14 sources shown): recall **0.75**. More sources alone do not explain
  this: flat hybrid at k = 12 is 0.48.

### 3. Pipeline evolution

Each step was driven by a failure measured in the previous run. Runs marked *console only* were
overwritten before run history was kept; their numbers come from the run's printed output and were
scored by an earlier scorer version.

#### 3.1 baseline-v1 — one query, flat top-10, one LLM call

| outcome exact | answerable / ambiguous / escalate | predicted outcomes |
|---|---|---|
| 0.19 | 0.33 / 0.20 / 0.00 | **known_issue 15 / 16**, info_request 1 |

Weaknesses (all measured):
* **"Similar report = known issue":** known_issue for 15/16 cases, including all 5 escalate cases and 4 of
  5 ambiguous ones — exactly what the brief warns about ("a similar report alone does not prove the same cause").
* **No fact / doc / hypothesis discipline:** 81% of hypotheses claimed `documented` while citing only
  other users' issues.
* **Unsupported confidence:** 4 replies recommend upgrading without any fix evidence; 4 wrong answers marked `high`.
* **Generic questions:** 2 checklist questions ("your Streamlit version, Python version, OS and browser"),
  1 asking for a version the report already gave.
* **Evidence:** retrieval recall 0.47; 5 of 15 labelled cases retrieved none of their relevant sources;
  only 1.9 citations per reply; 2 cases cite the same issue twice.

#### 3.2 baseline-v2 — same call + the shared rules text

Purpose: separate *better wording* from *better pipeline*. `agent/rules.py` is the exact rules text
the final pipeline uses ("a similar report is not proof…", "ask only when…", basis definitions, no
upgrade without a fix). It went through two revisions; baseline-v2 was re-run with each so it always
matches the final pipeline's rules.

| Version | outcome exact | ans / amb / esc | high-conf wrong | contract problems |
|---|---|---|---|---|
| rules r1 *(console only)* | 0.19 | 0.17 / 0.40 / 0.00 | 4 | 7 |
| rules r2 (current) | 0.31 | 0.17 / 0.80 / 0.00 | **6** | 6 |

Findings:
* Rules in the prompt fix the **ambiguous** bucket (0.20 → 0.80) and the basis labels (81% → 13%
  mislabelled), but **escalate stays at 0/5** (4 still called known_issue) and answerable *drops*
  (0.33 → 0.17).
* **More confident, not more right:** high-confidence wrong rises 4 → 6; 6 replies break the output
  contract (invalid `basis` values); checklist questions rise 2 → 3.
* Conclusion: wording alone is not enough → the decision must be enforced in code, and the evidence
  must be better.

#### 3.3 final-v1 — 3-call pipeline, rules r1 *(console only)*

Steps: (1) facts — code-parsed env + LLM symptoms/queries (an env value is kept only if its quote is in
the report); (2) grouped multi-query retrieval; (3) LLM assesses every source and lists hypotheses;
(4) decision rules in code; (5) LLM drafts the reply, code enforces outcome, basis, citations, questions.

| outcome exact | ans / amb / esc | predicted outcomes | retrieval recall |
|---|---|---|---|
| 0.31 | 0.00 / 1.00 / 0.00 | **info_request 16 / 16** | **0.75** (from 0.47) |

The retrieval gain was immediate and permanent (0.47 → 0.75, duplicate citations 2 → 0, basis
mislabels → 0, checklist/redundant questions → 0, upgrades without evidence → 0, high-confidence wrong → 0),
but the outcome collapsed to the opposite extreme. Diagnosis from the traces:
* The LLM itself proposed info_request in 15/16 cases (code downgraded only 1). Rule r1 "if several causes
  remain plausible → ask" is almost always true.
* Only **74 of 224** sources (33%) were assessed; the rest silently counted as background.
* **0** issue sources carried a maintainer reply: the one-chunk-per-source merge kept the issue *body*
  and dropped the comment where the maintainer answered, so the "maintainer statement" rule could never fire.

#### 3.4 final-v2 — signals + code decision table, rules r2, maintainer replies attached

Changes: (a) the earliest maintainer reply of the same thread is attached to each issue source, under the
**same leakage filters** (a test checks a later reply stays hidden) → 40 of 96 issue sources now carry
one (dev run of final-v4); (b) the LLM returns two signals (`blocking_unknowns`, `report_has_concrete_error_or_repro`) and a
**decision table in code** picks the outcome; (c) rules r2: ask only when missing information *blocks*
advice, escalate on a concrete unexplained error.

| outcome exact | ans / amb / esc | LLM-proposed exact | high-conf wrong |
|---|---|---|---|
| 0.38 | 0.67 / 0.20 / 0.20 | 0.44 | 3 |

Weakness: over-calls known_issue (7/16). The **code decision (0.38) was worse than the LLM's own
proposal (0.44)**, because (i) the maintainer rule ran *before* the blocking-unknowns check (c-12709,
c-13165, c-16570: the LLM correctly said "version is blocking", code still said known_issue);
(ii) "a maintainer commented" was treated as "a maintainer confirmed the cause" (c-13521, c-8149);
(iii) a general troubleshooting doc counted as "same cause" (c-5500, c-9781).

#### 3.5 final-v3 — order fixed, `resolves` required

Changes: blocking unknowns are checked before maintainer/docs support (only a newer fixing release note
with a known user version outranks them); qualifying evidence must be same-cause **and** `resolves`
(states both cause and fix for this exact problem); every label is listed in the prompt. Replaying just the
reordered rules over the saved v2 traces predicted 9/16 before spending anything; the real run gave 9/16.

| outcome exact | ans / amb / esc | LLM-proposed exact | high-conf wrong |
|---|---|---|---|
| 0.56 | 0.33 / **1.00** / 0.40 | 0.50 | **0** |

Remaining weaknesses: the LLM listed generic environment fields as "blocking" when nothing depended on
them (c-8149 → ask instead of escalate; 1 checklist question); a resolving source with no relation label
was ignored (c-7867); one upgrade flag was a false alarm (patch release 1.45.1 exists only in a maintainer
reply, not in the release notes).

#### 3.6 final-v4 — chosen

Changes (code only — no prompt change for facts/assessment): generic env fields (version, Python, OS,
browser) only block when a version-dependent fix exists; `resolves = true` counts even without a relation
label; upgrade advice allowed when a maintainer names the fixed version. Replaying v4's decision table on
the v3 traces predicted 11/16; the real run gave 11/16.

| outcome exact | ans / amb / esc | LLM-proposed exact | high-conf wrong | safety metrics |
|---|---|---|---|---|
| **0.69** | 0.50 / 1.00 / 0.60 | 0.50 | 0 | all 0 |

### 4. Full comparison

| Metric | baseline-v1 | baseline-v2 | final-v2 | final-v3 | **final-v4** |
|---|---|---|---|---|---|
| Outcome exact | 0.19 | 0.31 | 0.38 | 0.56 | **0.69** |
| Right kind (bucket_ok) | 0.44 | 0.50 | 0.38 | 0.56 | **0.69** |
| — answerable exact | 0.33 | 0.17 | **0.67** | 0.33 | 0.50 |
| — ambiguous exact | 0.20 | 0.80 | 0.20 | **1.00** | **1.00** |
| — escalate exact | 0.00 | 0.00 | 0.20 | 0.40 | **0.60** |
| LLM-proposed outcome exact | – | – | 0.44 | 0.50 | 0.50 |
| Retrieval recall | 0.47 | 0.47 | **0.75** | **0.75** | **0.75** |
| Cited recall | 0.36 | 0.33 | 0.49 | **0.49** | **0.49** |
| Citations per reply | 1.9 | 1.8 | 2.1 | 2.3 | 2.3 |
| Sources shown | 10 | 10 | 14 | 14 | 14 |
| Duplicate-citation cases | 2 | 1 | 0 | 0 | 0 |
| Basis mislabel rate | 0.81 | 0.13 | 0 | 0 | 0 |
| Questions per reply | 0.94 | 0.69 | 0.88 | 1.06 | 0.94 |
| Redundant-question cases | 1 | 2 | 0 | 0 | 0 |
| Checklist-question cases | 2 | 3 | 0 | 1 | **0** |
| Unsupported-upgrade cases | 4 | 2 | 0 | 0 | 0 |
| High-confidence wrong | 4 | 6 | 3 | 0 | 0 |
| Contract problems | 0 | 6 | 0 | 0 | 0 |
| LLM calls per case | 1 | 1 | 3 | 3 | 3 |
| Input / output tokens per case | 3,665 / 467 | 4,007 / 435 | 7,885 / 1,173 | 7,731 / 970 | 7,731 / 968 |
| Cost per case | $0.00083 | $0.00086 | $0.00189 | $0.00174 | $0.00174 |
| LLM latency per case | 6.8 s | 6.1 s | 19.6 s | 15.9 s | 16.0 s |

Confusion (predicted outcomes per bucket):

| Bucket | baseline-v1 | baseline-v2 | final-v4 |
|---|---|---|---|
| answerable (6) | known_issue 6 | known_issue 4, info_request 2 | known_issue 2, usage_guidance 1, info_request 2, escalate 1 |
| ambiguous (5) | known_issue 4, info_request 1 | info_request 4, known_issue 1 | **info_request 5** |
| escalate (5) | known_issue 5 | known_issue 4, info_request 1 | **escalate 3**, info_request 2 |

### 5. Per-case results (✓ = outcome exact)

| Case | Bucket | Gold | baseline-v1 | baseline-v2 | final-v2 | final-v3 | final-v4 |
|---|---|---|---|---|---|---|---|
| c-11647 | answerable | known_issue | ✓ | info_request | ✓ | ✓ | ✓ |
| c-12709 | ambiguous | info_request | known_issue | ✓ | known_issue | ✓ | ✓ |
| c-13102 | answerable | usage_guidance | known_issue | info_request | info_request | info_request | info_request |
| c-13165 | ambiguous | info_request | known_issue | ✓ | known_issue | ✓ | ✓ |
| c-13461 | answerable | known_issue | ✓ | ✓ | ✓ | ✓ | ✓ |
| c-13521 | escalate | escalate | known_issue | known_issue | known_issue | ✓ | ✓ |
| c-15326 | answerable | usage_guidance | known_issue | known_issue | ✓ | escalate | escalate |
| c-16030 | escalate | escalate | known_issue | known_issue | ✓ | ✓ | ✓ |
| c-16570 | ambiguous | info_request | known_issue | ✓ | known_issue | ✓ | ✓ |
| c-5500 | ambiguous | info_request | ✓ | ✓ | usage_guidance | ✓ | ✓ |
| c-6956 | answerable | usage_guidance | known_issue | known_issue | ✓ | info_request | info_request |
| c-7867 | answerable | usage_guidance | known_issue | known_issue | info_request | info_request | ✓ |
| c-8149 | escalate | escalate | known_issue | known_issue | known_issue | info_request | ✓ |
| c-8364 | escalate | escalate | known_issue | known_issue | info_request | info_request | info_request |
| c-9665 | ambiguous | info_request | known_issue | known_issue | ✓ | ✓ | ✓ |
| c-9781 | escalate | escalate | known_issue | info_request | usage_guidance | info_request | info_request |
| **Correct** | | | **3** | **5** | **6** | **9** | **11** |

### 6. Why final-v4

1. **Best outcome accuracy, in every bucket that matters:** 0.69 vs 0.19 / 0.31; ambiguous 1.00 and
   escalate 0.60, where both baselines score 0.00 on escalate.
2. **No regressions:** it is right on all 3 cases baseline-v1 got right and all 5 baseline-v2 got right
   (8–0 and 6–0, sign test p = 0.008 and 0.031).
3. **The pipeline, not the wording, makes the difference:** baseline-v2 uses the same rules text and
   reaches only 0.31. Within final-v4 the code decision (0.69) beats the LLM's own proposal (0.50):
   code overrode the LLM in 6 cases — 4 fixed (c-16030, c-7867, c-8149, c-9665), 2 broken (c-15326, c-6956), net +2.
4. **Every safety metric is zero:** 0 unsupported upgrades, 0 high-confidence wrong, 0 basis mislabels,
   0 checklist/redundant questions, 0 duplicate citations, 0 contract problems, 0 errors.
5. **Better evidence:** retrieval recall 0.47 → 0.75, cited recall 0.36 → 0.49.
6. **Cost is affordable:** 2.1× the baseline ($0.0017 vs $0.0008 per case; $0.028 for all 16 dev
   cases). The whole project has spent **$0.152 of the $5 budget** so far (chat $0.126, embeddings $0.026).
   Latency 16 s vs 7 s per case.
7. **Explainable:** every run file has a `trace` per case (facts, queries, per-source relations, signals,
   the decision rule that fired, and every correction applied to the draft).

### 7. Failure analysis (final-v4, 5 misses)

| Case | Gold → predicted | Retrieval recall | Cause |
|---|---|---|---|
| c-13102 | usage_guidance → info_request | 0.50 | The key release note (1.51: Python 3.9 support dropped) was **not retrieved**; the sanity-checks page was shown but judged background/irrelevant |
| c-15326 | usage_guidance → escalate | 0.33 | Same topic: neither the 1.51 release note nor the Community Cloud "upgrade Python" page was retrieved; code's "concrete error → escalate" rule fired |
| c-6956 | usage_guidance → info_request | 0.50 | A resolving doc was found, but the LLM listed "virtual environment" as blocking; code respected it (the LLM had proposed the right answer) |
| c-8364 | escalate → info_request | 1.00 | Both labelled sources shown (`server.headless`, architecture page) but judged background; no repro signal → the LLM's info_request was kept |
| c-9781 | escalate → info_request | 1.00 | The earlier issue #4516 was judged same cause but not resolving (correct — it was never resolved); no repro signal → info_request kept instead of escalate |

* Two misses are **retrieval** problems, both caused by the same missing release note (1.51, topic
  *python-3.9-dropped*): a symptom-worded report ("pip can't find 1.51", "Cloud uses the wrong Python")
  does not match the release-note wording.
* Three are **decision** problems: the right sources were shown (recall 1.00 in c-8364 / c-9781), but the
  relation/signal labels did not trigger the right rule. In c-8364 and c-9781 the gap is "related evidence,
  nothing resolves it, no explicit repro" — the table falls back to the LLM's info_request where escalate was right.
* Code overrides that hurt: c-15326 and c-6956 (above).

### 8. Limitations and threats to validity

* **Small sample:** 16 dev cases; one case = 6 points of accuracy.
* **Tuned on dev:** four rounds of changes (final-v1 → v4) were made after looking at dev results.
  Every change is a general rule, not a case-specific patch, and each was checked by replaying the
  decision table on saved traces first — but over-fitting to these 16 cases is possible. The **test split
  is the real measure** and has not been touched.
* **Gold labels are drafts:** written by an LLM from the full threads (`reviewed_by: null`), and the
  `relevant_sources` were picked from the retriever's own results (recall may be optimistic). A team
  review is pending; all runs can be re-scored for $0 after it (`--rescore`).
* **Not yet measured:** `must_not` violations and reply quality need an LLM judge with a published
  prompt, checked against human labels.
* **Console-only runs:** baseline-v2 with rules r1 and final-v1 were overwritten before run history
  existed; their numbers come from printed output and an earlier scorer version.
* **Docs snapshot** is from 2026 and treated as timeless; patch releases (e.g. 1.45.1) are missing from
  the release notes and only appear in maintainer replies.

### 9. Reproduce

```bash
export PYTHONPATH=src
python -m pytest -q tests                                   # 42 offline tests
python -m ssa.eval.retrieval_gold --split dev
python -m ssa.eval.p1 --system baseline    --split dev
python -m ssa.eval.p1 --system baseline-v2 --split dev
python -m ssa.eval.p1 --system final       --split dev
# offline, $0, from the committed LLM cache:
LLM_CACHE_MODE=replay python -m ssa.eval.p1 --system final --split dev
# re-score saved runs after a gold change:
python -m ssa.eval.p1 --system final --split dev --rescore
```

Run files: `data/eval/runs/history/p1_<system>_<version>_dev.json` (per-case outputs, sources, scores,
cost and the full `trace`). Cost ledger: `data/usage/ledger.jsonl`.

## Problem 2 — Following a case with a reliable agent

*To be written.*
