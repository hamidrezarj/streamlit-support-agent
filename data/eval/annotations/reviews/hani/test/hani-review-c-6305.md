Reviewer: Hani
Review date: 2026-10-09
Case ID: c-6305
Split: test

1. My initial decision (before reading the draft label)
Outcome: info_request
Reason: The inner baseUrlPath value and two-stage topology are supplied. Request missing configuration for both proxy hops and the actual failing browser path/query; a code 1006 is not a root cause. The 1.20 message fix was released on March 9, before intake, but its relevance to a user still running 1.11.1 is not established.

2. Draft label
Original outcome: info_request

3. Final review decision
Verdict: Change
Recommended outcome: info_request
Reason for accepting or changing the draft: The inner baseUrlPath value and two-stage topology are supplied. Request missing configuration for both proxy hops and the actual failing browser path/query; a code 1006 is not a root cause. The 1.20 message fix was released on March 9, before intake, but its relevance to a user still running 1.11.1 is not established.

4. Other corrections
Write "No change" where appropriate.

- missing_info: Inner nginx and outer HTTP proxy rewrite/upgrade configuration — Tests both hops and the external-to-internal path mapping.; Actual failing browser request URL/query, response and close timing — Separates path/query loss, handshake and post-connect failure.
- acceptable_next_steps: Inspect both proxy-hop configurations, query preservation and WebSocket upgrade end to end.; Compare direct and proxied access using the supplied /dashboard base path and a minimal app.
- must_not: Ask again what server.baseUrlPath is without acknowledging the supplied value.; Attribute 1.11.1 behavior to the 1.18 rename.; Promise upgrading to 1.20 fixes this case without establishing the same regression.
- relevant_sources: No change proposed.
- bucket: No change.

5. Review status
Human review complete: YES
Unresolved questions: 