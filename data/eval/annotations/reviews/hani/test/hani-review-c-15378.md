Reviewer: Hani
Review date: 2026-10-09
Case ID: c-15378
Split: test

1. My initial decision (before reading the draft label)
Outcome: info_request
Reason: The same machine works at 127.0.0.1 but not localhost, with explicit WebSocket failure already supplied. A targeted IPv4/IPv6, proxy-bypass and pre-Starlette comparison is more discriminating than immediately confirming a library regression. Escalation with the minimal example remains reasonable.

2. Draft label
Original outcome: escalate

3. Final review decision
Verdict: Needs discussion
Recommended outcome: info_request
Reason for accepting or changing the draft: The same machine works at 127.0.0.1 but not localhost, with explicit WebSocket failure already supplied. A targeted IPv4/IPv6, proxy-bypass and pre-Starlette comparison is more discriminating than immediately confirming a library regression. Escalation with the minimal example remains reasonable.

4. Other corrections
Write "No change" where appropriate.

- missing_info: localhost resolution/binding and proxy bypass settings — Separates IPv4/IPv6 or corporate interception from a server regression.; Same minimal example on pre-Starlette version in the same network — Tests the claimed version boundary.
- acceptable_next_steps: Compare localhost versus 127.0.0.1, binding and proxy-bypass behavior with the supplied minimal app.; Compare the earlier version in the same environment and escalate reproducible differences with the existing console error.
- must_not: Ask for the same browser console WebSocket error already pasted.; Claim later authentication/cookie analysis is initial knowledge.; Declare a confirmed Streamlit regression solely from private-versus-corporate behavior.
- relevant_sources: No change proposed.
- bucket: Change from escalate to ambiguous.

5. Review status
Human review complete: YES
Unresolved questions: 