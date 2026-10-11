Reviewer: Hani
Review date: 2026-10-11
Scenario ID: s-8364
Case ID: c-8364
Split: dev

1. My proposed decision
Recommended final status: escalated

2. Draft expectation
Original status: escalated
Original comments_posted: 2

3. Final review decision
Verdict: Change
Status decision: Keep the original status; revise the test details
Reason: The reported headless-mode/external-IP investigation supports maintainer investigation. The restart test must verify durable recovery of the same pending proposal.

4. Other corrections
- scenario_steps: Save the pending proposal identity, payload and case revision, restart, then approve that unchanged proposal.
- approved_actions: Final approval must cover the reply, escalation status and needs-maintainer label.
- must_not: No write before approval, lost pending proposal, duplicate comment or presentation of the reported diagnosis as universally confirmed.
- expected_final: Store escalated, needs-maintainer and two successful approved comments. Check the pending proposal before and after restart; this fixture does not test a crash after a write commits.

5. Review status
Human review complete: YES
Unresolved questions: 
