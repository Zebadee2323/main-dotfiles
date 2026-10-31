# Direct agent conversation examples

Apply the communication-style contract in SKILL.md before substantive internal sends.
After the required identity prefix, use the recipient's checked name:

- Supervisor → worker: "Hey Emma, please implement the report module and cover empty input."
- Worker → reviewer: "Hey Tess, my changes are frozen and ready for your review."
- Reviewer → worker: "Hey Emma, please handle the empty-record case and add coverage."
- Reviewer → supervisor: "Hey Jaden, I’ve approved the frozen snapshot; the evidence is in my report."
- Supervisor → worker: "Hey Emma, please run the combined tests on the merged commit."

These are short audible/readable previews. Send the detailed task or evidence
through tuios immediately afterward. Avoid duplicate narration of routine tools.
Only the supervisor addresses the user with key delivery events. Workers and
reviewers speak to the target agent even though the user can hear the exchange.

Acknowledgment-only replies such as "understood; I’ll run those checks" go
through internal mail without `agent-comms send`. Keep required handshakes;
announce new findings or results in a mixed reply, not its receipt acknowledgment.

For user input, give a fuller acknowledgment. Example: "Oli, I understand that
combined test execution should belong to a worker. I’ll update the supervisor
and worker contracts, preserve reviewer assessment, and sync the installed
skills after validation." Follow the active checked identity prefix rules.
