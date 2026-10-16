# Streamlit Support Agent — Report

## Problem 1 — Investigating a report and proposing a path

> **Status:** dev split only (16 cases). The test split (16 cases) has **not** been run; it will be run once,
> with the frozen system, for the final numbers. All numbers below come from the saved run files in
> `data/eval/runs/history/`, re-scored with the same scorer (`ssa.eval.p1 --rescore`).
> **Gold:** sections 2–8 were written while the gold was an LLM draft; section 1b reports the human review
> (2026-10-09) and the re-scored results on the **reviewed** gold, which are the numbers that count.

### TL;DR

> **Held-out test (§1c):** final-v4 0.375 exact / 0.44 acceptable vs baselines 0.125 / 0.19; same ranking as dev,
> not significant on 16 cases; 0 high-confidence wrong answers on both splits.

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

### 1b. Human gold review and re-scored results

All 32 cases were reviewed by a team member (Hani) — `data/eval/annotations/reviews/hani/`, parsed by
`ssa.eval.merge_reviews`, applied by `ssa.eval.build_cases` (the LLM draft is kept under `gold.draft`).
Protocol: the reviewer first decided the outcome **blind** (only the visible report, before reading the
draft), then compared with the draft and edited the fields. Test-split reviews were used only to correct
gold, never to change a system.

| Review statistic | Value |
|---|---|
| Cases reviewed | 32 / 32 |
| Blind human outcome = LLM-draft outcome | 26 / 32 (81%), **Cohen's κ = 0.74** |
| Outcome changed by the reviewer | 6 (dev: c-6956, c-8149, c-9781; test: c-6095, c-6224, c-15378) |
| Bucket changed | 5 → dev now 6 answerable / 7 ambiguous / 3 escalate; test 4 / 8 / 4 |
| Fields edited | must_not 28, acceptable_next_steps 32, missing_info 10, hypotheses 4, summary 3, reference 1 |
| relevant_sources edited | 0 (the retriever-bias limitation in §8 remains) |

What the reviewer corrected is the failure mode this project is about: the draft over-trusted similar
reports and later information (c-6224 / c-6956: the report already says the obvious fix was tried;
c-9781 / c-15378: the cause was confirmed only after intake). Seven cases were marked "needs discussion";
the team recorded the alternatives the reviewer called defensible as `acceptable_outcomes`
(`data/eval/annotations/review_decisions.json`), scored as **outcome acceptable** next to **outcome exact**.

Re-scored on the reviewed gold (dev, 16 cases; same saved outputs, no new model calls):

| Version | exact (draft gold) | **exact (reviewed)** | **acceptable** | answerable / ambiguous / escalate (exact) | high-conf wrong |
|---|---|---|---|---|---|
| baseline-v1 | 0.19 | 0.25 | 0.25 | 0.50 / 0.14 / 0.00 | 3 |
| baseline-v2 | 0.31 | 0.44 | 0.44 | 0.33 / 0.71 / 0.00 | 5 |
| final-v2 | 0.38 | 0.38 | 0.44 | 0.67 / 0.14 / 0.33 | 3 |
| final-v3 | 0.56 | 0.69 | 0.75 | 0.33 / 1.00 / 0.67 | 0 |
| **final-v4** | 0.69 | **0.75** | **0.88** | 0.50 / 1.00 / 0.67 | 0 |

Paired comparison of final-v4 (two-sided sign test on the cases where the two systems differ):
vs baseline-v1 — exact 9 wins / 1 loss (p = 0.02), acceptable 10 / 0 (p = 0.002);
vs baseline-v2 — exact 6 / 1 (**p = 0.13, not significant on 16 cases**), acceptable 7 / 0 (p = 0.02).
The ranking of all versions is unchanged by the review; final-v4's remaining misses on the reviewed gold
are c-13102 and c-15326 (both the missing 1.51 release note, §7).

### 1c. Held-out test split (run once, 2026-10-14, after the freeze)

The systems were frozen (commit `c8eeef4`) and run **once** on the 16 test cases; nothing was changed
afterwards. All calls were **live** (0 replayed). Gold = Hani's reviewed gold.

| | baseline-v1 | baseline-v2 | **final-v4** |
|---|---|---|---|
| Outcome exact | 0.125 (2/16) | 0.188 (3/16) | **0.375 (6/16)** |
| Outcome acceptable | 0.125 | 0.188 | **0.438 (7/16)** |
| Right kind (bucket_ok) | 0.25 | 0.31 | **0.44** |
| Answerable / ambiguous / escalate exact (n = 4 / 8 / 4) | 0.50 / 0.00 / 0.00 | 0.50 / 0.13 / 0.00 | 0.25 / 0.38 / 0.50 |
| Predicted outcomes | `known_issue` 15/16 | `known_issue` 14/16 | 7 escalate, 4 info_request, 3 usage, 2 known |
| Retrieval recall (labelled sources shown) | 0.37 | 0.37 | **0.65** |
| Cited recall | 0.24 | 0.14 | **0.43** |
| Wrong "documented" basis labels | 75% | 31% | **0%** |
| Upgrade advice without evidence (cases) | 7 | 5 | **2** |
| High-confidence wrong answers | 3 | 6 | **0** |
| Output-contract problems | 0 | 6 | 0 |
| Cost per case (live) | $0.00096 | $0.00097 | $0.00193 (2.0×) |
| LLM latency per case | 10.4 s | 8.0 s | 17.6 s |

Paired sign test (cases where the systems differ): final vs baseline-v1 exact 5 wins / 1 loss
(p = 0.22), acceptable 6 / 1 (p = 0.13); vs baseline-v2 exact 4 / 1 (p = 0.38). **The ranking from dev
holds on test, but none of the differences is significant on 16 cases**, and all three systems score much
lower than on dev (final 0.75 → 0.375, baseline-v2 0.44 → 0.19). The safety-type metrics (0 high-confidence
wrong, 0% basis mislabels, retrieval recall +0.28) hold up better than outcome accuracy.

**Main test failure — the code escalation rule over-fires.** The rule *"concrete error/repro and no doc or
known fix → escalate"* (added in final-v2 because dev escalate cases were answered as "ask") fired on
8 test cases: 2 correct (c-14432, c-16341), 1 acceptable (c-8321), **5 wrong** (c-11509, c-6224, c-6305,
c-9140: gold `info_request`; c-7856: gold `usage_guidance`). Many *ambiguous* test reports contain a
traceback, yet still need one discriminating fact from the user; a traceback is not a reason to involve a
maintainer. On 3 of these cases the LLM's own proposal (`info_request`) was right and the code overrode it:
**LLM-proposed exact 0.50 vs code decision 0.375** on test (dev: 0.50 vs 0.75). Post-hoc count, *not* a
result (the test split is now seen): keeping the LLM's `info_request` when it proposes a discriminating
check would give 8/16. This is the first change for a next version, to be validated on new cases.

Other misses: c-11670 / c-5793 (gold escalate → `usage_guidance`: the model graded a general docs page as
resolving), c-6095 (gold usage → `info_request`: no qualifying doc retrieved), c-9176 (gold usage →
`known_issue` from a maintainer statement). Run files: `data/eval/runs/p1_<system>_test.json`.

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
  decision table on saved traces first — but over-fitting to these 16 cases is possible. The held-out test split
  (§1c) confirms it: exact 0.75 → 0.375, with one dev-motivated rule (escalate on a concrete error)
  causing 5 of the 10 test misses.
* **Gold labels:** drafted by an LLM from the full threads, then reviewed by one team member (§1b; κ = 0.74
  between the blind human decision and the draft). Only one reviewer, so no human–human agreement is
  reported. `relevant_sources` were picked from the retriever's own results and were not changed by the
  review, so retrieval recall may be optimistic.
* **Not yet measured:** `must_not` violations and reply quality need an LLM judge with a published
  prompt, checked against human labels.
* **Console-only runs:** baseline-v2 with rules r1 and final-v1 were overwritten before run history
  existed; their numbers come from printed output and an earlier scorer version.
* **Docs snapshot** is from 2026 and treated as timeless; patch releases (e.g. 1.45.1) are missing from
  the release notes and only appear in maintainer replies.

### 9. Reproduce

```bash
export PYTHONPATH=src
python -m pytest -q tests                                   # 85 offline tests
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

### TL;DR

| Test split (8 scenarios, held-out, run once) | baseline (memoryless + naive executor) | **agent-v3** |
|---|---|---|
| **Safety invariant violations** | **3** (duplicate post, stale execution, rejected text posted) | **0** |
| Facts remembered / corrections kept as history | 0/2 / 0/2 | **2/2 / 2/2** |
| Step-limit behaviour (stop recorded, no LLM call after limit, status kept) | 0/3 (no limits) | **3/3** |
| Final status as expected | 3/7 | 2/7 (over-escalation, see §P2.6) |
| All code checks | 51/67 | **70/75** |
| Cost per agent turn | $0.0009 | $0.0018 |

On dev (5 scenarios): agent-v3 **50/52** checks, 0 violations, status 4/5; baseline 33/46, 1 duplicate post,
status 1/5. The agent's **safety and memory** guarantees held on every scenario of both splits; its **decision
quality** (which status to propose) inherits Problem 1's over-escalation on test.

### P2.1 Design (`docs/architecture_p2.png`)

A deterministic orchestrator (`ssa.agent.orchestrator.Agent`) with LLM steps; the model never writes to the
tracker. One agent turn:

1. **read** the ticket (local SQLite tracker, `ssa.tracker.store`), new user messages since the last turn;
2. **update memory** (LLM, `SYS_UPDATE`): facts with an exact **quote** from the user's message (no quote in the
   text → fact rejected), corrections **supersede** the old value (history kept), things the user tried and
   whether *our* suggestion failed, explicit confirmation that the problem is solved;
3. **analyse** with the Problem 1 final-v4 pipeline on the report + memory (memory facts first), with the
   leakage cut-off = newest *user-side* timestamp;
4. **multi-turn rules in code** (decision hook): a fix *we* suggested failed and nothing new resolves it →
   escalate; `closed_resolved` only after a quoted user confirmation;
5. **propose** actions (`post_comment`, `set_status`, `set_labels`) — stored as a proposal with a content
   hash and the ticket version it was made for. **Nothing is visible yet.**

The **maintainer gate** (`ssa.agent.tools.MaintainerGate`) is the only path to the tracker:

| Guarantee | How (code) |
|---|---|
| no action without approval | tools only *propose*; `approve / edit / reject` execute or discard |
| approval bound to what was seen | approve(case, proposal id, **hash**); wrong case / hash → refused |
| no stale execution | ticket version changed since the proposal (e.g. a new user message) → `StaleProposal`, re-plan |
| edit = new proposal | edited text posted **verbatim**, origin `maintainer_edit` |
| reject = nothing written | reason stored and given to the next draft |
| no duplicate writes | every write keyed `<proposal>:<i>`; retries / duplicate requests return the stored result |
| verified results | read the ticket back after writing; failure is never reported as success |
| survives a crash | proposals + memory in SQLite; the pending proposal is found again after restart |
| bounded work | 8 steps per turn, 30 per case (LLM call or tool = 1 step); at the limit → stop, escalation *proposal*, no status change |
| prompt injection | issue / comment / doc text is data; it cannot call the gate |
| audit | JSONL per action (secrets masked) — sample traces in `data/eval/runs/p2_*/*/audit.jsonl` |

**Baseline:** `MemorylessAgent` (Problem 1 baseline on the whole thread each turn) + `NaiveGate` (executes the last
proposal without case / hash / version checks, no idempotency keys, no step limits). Same tracker, same model.

### P2.2 Scenarios (13: 5 dev, 8 test)

Built from real issue threads: the user turns are the issue author's **real** follow-up comments
(`origin: real`, with their original timestamps); two dev turns are designed (`s-5500` correction). Reviewed
by Hani (`data/eval/annotations/reviews/hani/scenarios/`, decisions in `scenario_review_decisions.json`):
expected statuses / facts corrected, acceptable alternative statuses, exact maintainer edit text, an explicit
shared event schedule for the interleaved pair, and two variants.

| Scenario | Split | What it tests |
|---|---|---|
| s-12709 | dev | real follow-ups refute a hypothesis; facts (version, Community Cloud, "8 out of 10") |
| s-5500 | dev | user **correction** (direct IP → nginx under /st/); old value kept as history |
| s-7867 | dev | maintainer **edit**, posted verbatim; no `type:bug` label, not closed |
| s-8364 | dev | **crash while awaiting approval** → restart → same pending proposal (id, hash, version, actions) |
| s-9781 | dev | **duplicate approval request** → one post; close only after the user confirms |
| s-11500 | test | user correction arrives **before** approval → stale proposal refused, re-plan |
| s-9140 + s-11509 | test | two cases **interleaved** in one process: no fact / approval crosses cases |
| s-15378 | test | real follow-ups |
| s-15378-limit | test | **step limit** (7 per case): stop recorded, no LLM call after it, status unchanged |
| s-7076 | test | maintainer **reject** → nothing written, reason used in the next draft |
| s-9176 | test | tool fails **before** writing → one retry with the same key |
| s-9176-lost | test | write succeeds but the response is **lost** → retry returns the stored result, no 2nd post |

### P2.3 Evaluation: checks on stored state, not on model text (`ssa.eval.p2`)

The runner replays every step against the real tracker, memory and gate and then checks the **stored** state:
expected status (or an accepted alternative), number of agent comments, remembered facts (with history),
labels, edited text verbatim, step-limit behaviour, and the invariants — no duplicate execution, no stale
execution, rejected text never in the tracker, no write from a non-executed proposal, pending proposal
survives restart unchanged, closed only after confirmation, every fact quotes **this** case (provenance), no
approval / fact crossing cases, step budget kept. Text-level `must_not` items are collected for a judge
(not run; see limitations).

### P2.4 Results

| | Dev baseline | Dev **agent-v3** | Test baseline | Test **agent-v3** |
|---|---|---|---|---|
| Scenarios passing all checks | 1/5 | **3/5** | 2/8 | **3/8** |
| Code checks | 33/46 | **50/52** | 51/67 | **70/75** |
| Invariant violations | 1 | **0** | 3 | **0** |
| Status as expected | 1/5 | **4/5** | **3/7** | 2/7 |
| Facts / superseded | 0/5 / 0/1 | **4/5 / 1/1** | 0/2 / 0/2 | **2/2 / 2/2** |
| Repeated identical replies | 1 | **0** | 4 | 1 |
| Agent turns | 10 | 10 | 18 | 18 |
| Cost: live + replayed USD | 0.0039 + 0.0039 | 0.0100 + 0.0061 | 0.0083 + 0.0082 | 0.0277 + 0.0054 |
| Cost per turn | $0.0008 | $0.0016 | $0.0009 | $0.0018 |

(Check totals differ because some checks exist only for the final system, e.g. keyed writes.)
Baseline violations on test, each a real-world incident: **s-9176-lost** — the response of a successful post was
lost and the naive retry posted the comment twice; **s-11500** — the user corrected the version before the
maintainer approved, and the old reply was executed anyway; **s-7076** — the memoryless baseline re-drafted the
same text after the maintainer rejected it, and it was posted.

### P2.5 What changed during development (effective changes)

| Version | Change | Effect |
|---|---|---|
| agent-v1 | orchestrator + gate + memory | dev: hook fired on the user's *own* failed attempts → wrong escalations |
| agent-v2 | "our fix failed" counts only for fixes **we** suggested; env template parsed (no fake corrections) | dev 34/37 (old fixtures), 0 violations |
| review | Hani's scenario review: stricter fixtures and new checks (pending identity, provenance, no write from unexecuted proposals, step limit) | dev 47/52 |
| **agent-v3** | **leakage fix:** retrieval cut-off from user-side text only (agent posts carry today's date, which let turn ≥ 2 retrieve 2026 release notes for a 2024 case — found in s-8364, where the agent cited a 1.58.0 fix); memory records failure frequency and test results | dev **50/52**; s-8364 now escalates correctly |

### P2.6 Failure analysis

* **Over-escalation on test (all 5 status misses).** The agent proposed `escalated` where the gold expects
  `awaiting_user` (s-11500, s-15378) or `answered` (s-11509, s-9176, s-9176-lost). The audit log names the rule
  every time: *"concrete error/repro, no doc or known fix → escalate"* — the same Problem 1 rule that over-fires
  on test (§1c). Safe but unhelpful: the maintainer sees a proposal they would edit. Fix candidate (not applied,
  test seen): keep the LLM's `info_request` when it names a discriminating check.
* **s-5500 (dev):** the correction was stored and the old value kept as history, but "direct IP works" was not
  stored as its own fact, even after the prompt change.
* **s-7867 (dev):** the user had already shown that `minimal` works; the agent still asked for information
  because no document directly states the difference between `viewer` and `minimal`.
* **Found by the checks, fixed:** the leakage bug above; the hook firing on user-tried fixes (v1).

### P2.7 Limitations

* 13 scenarios (5 / 8) are few; one reviewer; real follow-ups only exist where the issue author replied.
* The maintainer is simulated by the scenario (approve / edit / reject at fixed points).
* No ablation run (memory off + safe gate) — the comparison mixes the memory and gate effects; the invariant
  violations are attributable to the gate (they are about execution), the fact checks to memory.
* `must_not` text items are not scored (no LLM judge was run).
* Local SQLite tracker, not GitHub; the tool layer is typed and could be swapped for a real API.

### P2.8 Reproduce

```bash
export PYTHONPATH=src
python -m ssa.eval.p2 --system final    --split dev
python -m ssa.eval.p2 --system baseline --split dev
LLM_CACHE_MODE=replay python -m ssa.eval.p2 --system final --split dev     # offline, $0
python -m ssa.cli tracker reset                                              # empty tracker + memory
```

Run files: `data/eval/runs/p2_<system>_<split>.json` and per-scenario folders with `audit.jsonl`,
`tracker.sqlite`, `state.sqlite` (sample traces).

---

## Cost report (whole project, from `data/usage/ledger.jsonl`)

Budget: $5 for the team. **Spent: $0.31** (live calls only; replayed calls cost $0 and are not in the ledger).

| Item | Calls | Tokens in | Tokens out | USD |
|---|---|---|---|---|
| Embeddings — index (5,990 chunks, once) | 71 batches | 1,278,565 | — | 0.026 |
| Embeddings — queries | 339 | 51,792 | — | 0.001 |
| Chat — final pipeline (P1 dev iterations v1–v4 + test, P2 analysis) | 279 | 817,922 | 101,280 | 0.184 |
| Chat — baseline | 57 | 220,274 | 27,703 | 0.050 |
| Chat — baseline-v2 | 48 | 200,343 | 22,176 | 0.043 |
| Chat — agent memory updates (P2) | 28 | 14,763 | 4,272 | 0.005 |
| Smoke test | 1 | 39 | 16 | 0.000 |
| **Total** | **823** | **2,583,698** | **155,447** | **0.308** |

Model `gpt-4o-mini` (chat) and `text-embedding-3-small` via the Metis gateway; prices are OpenAI list prices
(the Metis panel is the source of truth). Per unit: P1 case $0.001 (baselines) vs $0.002 (final);
P2 turn $0.0009 vs $0.0018. Every run file separates `live_calls` / `replayed_calls` and `usd_live` /
`usd_replayed`; the committed LLM cache (`data/llm_cache/chat.sqlite`) re-runs all evaluations offline for $0.

## Domain limits

Running and deploying Streamlit apps only (server, proxies, WebSockets, Docker, config, Community Cloud);
546 issues from 2021 onwards. Docs are a 2026 snapshot treated as timeless; patch releases are missing from
the release notes; the agent answers in English; reports outside the domain are expected to be escalated.

