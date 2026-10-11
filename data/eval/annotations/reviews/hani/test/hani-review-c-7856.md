Reviewer: Hani
Review date: 2026-10-09
Case ID: c-7856
Split: test

1. My initial decision (before reading the draft label)
Outcome: usage_guidance
Reason: The configuration already includes HTTP/1.1 and Upgrade/Connection headers. Its trailing-slash proxy location plus logged 301 explains the concrete mismatch under nginx documented behavior.

2. Draft label
Original outcome: usage_guidance

3. Final review decision
Verdict: Change
Recommended outcome: usage_guidance
Reason for accepting or changing the draft: The configuration already includes HTTP/1.1 and Upgrade/Connection headers. Its trailing-slash proxy location plus logged 301 explains the concrete mismatch under nginx documented behavior.

4. Other corrections
Write "No change" where appropriate.

- missing_info: No change proposed.
- acceptable_next_steps: Use an exact or appropriate slashless /_stcore/stream location and preserve WebSocket headers and upstream path.; Verify the request gets a 101 handshake instead of a 301 while direct NodePort behavior stays working.
- must_not: Ask again for nginx configuration.; Say the provided WebSocket location lacks Upgrade/Connection headers.; Claim Streamlit removed /_stcore/stream.
- relevant_sources: No change proposed.
- bucket: No change.

Additional notes:
- hypotheses: nginx slash-ended location redirects the slashless WebSocket endpoint with 301.

5. Review status
Human review complete: YES
Unresolved questions: 
