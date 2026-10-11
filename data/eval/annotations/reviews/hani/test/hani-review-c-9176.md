Reviewer: Hani
Review date: 2026-10-09
Case ID: c-9176
Split: test

1. My initial decision (before reading the draft label)
Outcome: usage_guidance
Reason: The unsupported configuration option is explicit and the user cannot modify the host launch command. Explain configuration precedence and where to remove the option; route host-injected CLI flags to Posit rather than pretending an env override always wins.

2. Draft label
Original outcome: usage_guidance

3. Final review decision
Verdict: Change
Recommended outcome: usage_guidance
Reason for accepting or changing the draft: The unsupported configuration option is explicit and the user cannot modify the host launch command. Explain configuration precedence and where to remove the option; route host-injected CLI flags to Posit rather than pretending an env override always wins.

4. Other corrections
Write "No change" where appropriate.

- missing_info: No change proposed.
- acceptable_next_steps: Check and remove the obsolete option from user-owned config/environment if present.; If the hosting launcher injects it, contact Posit to remove/update the launch flag; explain that config/env may not override CLI.
- must_not: Tell the user to edit a launch command they say they cannot access.; Promise an empty config file overrides a host-injected CLI option.; Escalate as a confirmed Streamlit defect.
- relevant_sources: No change proposed.
- bucket: No change.

5. Review status
Human review complete: YES
Unresolved questions: 
