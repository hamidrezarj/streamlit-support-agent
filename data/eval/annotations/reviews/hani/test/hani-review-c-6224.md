Reviewer: Hani
Review date: 2026-10-09
Case ID: c-6224
Split: test

1. My initial decision (before reading the draft label)
Outcome: info_request
Reason: The visible report explicitly says sticky sessions were enabled with no luck. A similar multi-replica case is not proof of the same cause. Validate actual upload/WebSocket affinity, instance routing, restarts and LB logs before declaring the known limitation the full explanation.

2. Draft label
Original outcome: known_issue

3. Final review decision
Verdict: Change
Recommended outcome: info_request
Reason for accepting or changing the draft: The visible report explicitly says sticky sessions were enabled with no luck. A similar multi-replica case is not proof of the same cause. Validate actual upload/WebSocket affinity, instance routing, restarts and LB logs before declaring the known limitation the full explanation.

4. Other corrections
Write "No change" where appropriate.

- missing_info: Actual affinity cookie/routing for upload and WebSocket requests — Verifies that the already-attempted sticky-session configuration is effective.; Instance IDs/restarts and 400/502 LB logs at upload failure — Separates cross-instance session loss from gateway/backend failure.
- acceptable_next_steps: Acknowledge the attempted sticky sessions and verify upload/WebSocket requests reach the same live backend.; Compare a single-instance deployment and inspect failed upload/LB logs, including size/timeouts as hypotheses.
- must_not: Simply repeat enable sticky sessions without acknowledging the previous attempt.; Treat 400 and 502 as proof of one identical root cause.
- relevant_sources: No change proposed.
- bucket: Change from answerable to ambiguous.

5. Review status
Human review complete: YES
Unresolved questions: 
