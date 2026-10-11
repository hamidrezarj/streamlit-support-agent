Reviewer: Hani
Review date: 2026-10-11
Scenario ID: s-11509
Case ID: c-11509
Split: test

1. My proposed decision
Recommended final status: answered

2. Draft expectation
Original status: answered
Original comments_posted: 2

3. Final review decision
Verdict: Change
Status decision: Keep the original status; revise the test details
Reason: The user explains the bootstrap.run configuration behavior, so an acknowledgement/explanation is reasonable. The joint case-switch test needs explicit event routing.

4. Other corrections
- scenario_steps: Define one shared event schedule with s-9140. Bind every user turn, agent turn and approval to its case, or provide explicit switch-back events.
- approved_actions: Bind approval to the intended case/proposal/revision even when another case is active.
- must_not: No facts or permissions imported from c-9140; check provenance rather than banning isolated words.
- expected_final: Store answered and two approved comments for c-11509 under the explicit schedule. Do not close as resolved automatically.

5. Review status
Human review complete: YES
Unresolved questions: 
