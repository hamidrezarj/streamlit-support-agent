# Next steps — Streamlit Support Agent (QBC13 final project)

Status as of **Sun 11 Oct 2026 (19 Mehr)**. Submission deadline: **Mon 20 Mehr (12 Oct)**.
Group presentation: Tue/Wed 21–22 Mehr.

## Where we are

| Area | State |
|---|---|
| Data, index, eval set | done — 546 issues, 5,990 chunks, 32 cases (16 dev / 16 test), 11 scenarios |
| Gold labels | **cases reviewed by Hani** (κ = 0.74 blind human vs LLM draft); scenarios **not yet reviewed** |
| Problem 1 | final-v4 chosen — dev 0.75 exact / 0.88 acceptable (baseline-v1 0.25, baseline-v2 0.44) |
| Problem 2 | agent-v3 (retrieval cut-off fix: agent posts no longer open the index to future docs; memory records failure frequency + test results) — dev re-run pending. agent-v2 numbers were affected by this leak on turns after the first post |
| Interfaces | CLI (`python -m ssa.cli ...`) and Streamlit demo UI (`streamlit run app/ui.py`, 3 tabs) |
| Test split | **not run yet** (systems not frozen) |
| Budget | ~$0.20 of $5 spent |

## Steps (in priority order)

| # | Step | Owner | Effort | Needed for |
|---|---|---|---|---|
| 0 | Commit the gold-review merge | Claude | done (`a2e9a54`) | — |
| 1 | **Minimal Streamlit UI** — 3 tabs: *Analyse a report* (P1), *Follow a case* (P2), *Results*; offline tests with `AppTest` + fake LLM | Claude | **done** (needs review on the Mac) | mentor demo |
| 2 | Try the UI on the Mac and rehearse the demo script below once | team | ~20 min | mentor demo |
| 3 | Scenario review sheets (same format as the case reviews) → human review of the 11 scenarios | Claude ~30 min, Hani ~1 h | | credibility of P2 numbers |
| 4 | P2 improvements from the discussion: real step-limit test in s-15378 (per-scenario `limits`), ablation row *memoryless agent + safe gate* (separates the effect of memory from the effect of the safety layer) | Claude | ~1 h | fair P2 comparison |
| 5 | **Freeze systems, run the test split once**: P1 baseline / baseline-v2 / final-v4; P2 baseline / ablation / agent-v3 | team (Mac) | ~30 min, ~$0.15 | final numbers |
| 6 | Report: P2 section, test numbers, cost report, sample traces (`audit.jsonl`), failure analysis; update both architecture diagrams; README reproduce check | Claude + team | ~2 h | submission |
| — | *Optional:* LLM judge for text must-nots + reply quality, published prompt, ~20 judgements checked by a human | Claude + Hani | ~2 h | only if time allows; otherwise stated as a limitation |

Cut order if time runs out: the optional judge first, then step 4.
Steps 1–2 are needed for the demo; steps 5–6 for the submission.

## Open decisions

- [ ] When is the mentor demo? (if today: UI first, everything else after)
- [ ] LLM judge: build it, or state "text must-nots not measured automatically" as a limitation?
- [ ] Debatable scenario expectations: s-12709 (escalated?), s-11509 (answered vs closed_resolved?), s-15378 (escalated after the failed setting?)

## Demo script for the mentor (~7 min)

1. **Results tab (1 min)** — P1: baseline 0.25 → final-v4 0.75 exact / 0.88 acceptable on the human-reviewed gold.
   P2: 0 safety violations vs 1 duplicate execution for the baseline.
2. **Analyse a report (2 min)**
   - c-12709 with *compare with baseline*: baseline calls a similar report a "known issue";
     final asks for the version because the fix depends on it.
   - c-8149: the code overrides the LLM — the decision panel shows the rule that fired.
   - Live: the brief's own example ("works on my laptop, stuck loading on the server, reinstalling did not help").
3. **Follow a case (4 min)** — open c-5500 → agent turn → approve →
   user: "direct IP, no proxy" → turn → user: "correction: it's behind nginx under /st/" → turn →
   memory shows the superseded value → reject a draft with a reason → the next draft follows it →
   *simulate restart* (pending proposal survives) → approve → *deliver the approval again* (already done, no second comment).

## Rules we keep

- The test split is run **once**, with frozen systems; test reviews are used only to correct gold.
- Every number in the report comes from a saved run file; replayed calls are reported separately from live calls.
- No secrets in code, logs or commits (secret scan before every commit).
