Reviewer: Hani
Review date: 2026-10-09
Case ID: c-6095
Split: test

1. My initial decision (before reading the draft label)
Outcome: usage_guidance
Reason: The user explicitly identifies the intentional endpoint rename and an LB rule restricted to the old path. Updating proxy rules is configuration guidance under the shared taxonomy, not an unconfirmed bug. known_issue is defensible only if the team explicitly includes documented migration limitations in that category.

2. Draft label
Original outcome: known_issue

3. Final review decision
Verdict: Change
Recommended outcome: usage_guidance
Reason for accepting or changing the draft: The user explicitly identifies the intentional endpoint rename and an LB rule restricted to the old path. Updating proxy rules is configuration guidance under the shared taxonomy, not an unconfirmed bug. known_issue is defensible only if the team explicitly includes documented migration limitations in that category.

4. Other corrections
Write "No change" where appropriate.

- missing_info: No change proposed.
- acceptable_next_steps: Update WebSocket routing/upgrade rules to the release-appropriate /_stcore/stream path.; Check related health routing and verify the handshake, preserving the deployment authentication policy.
- must_not: Promise rollback.; Escalate as a newly discovered Streamlit bug.
- relevant_sources: No change proposed.
- bucket: No change.

5. Review status
Human review complete: YES
Unresolved questions: 
