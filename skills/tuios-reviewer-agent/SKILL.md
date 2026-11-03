---
name: tuios-reviewer-agent
description: Review a tuios worker assignment in the worker session, run a scoped review/revise loop directly with the worker, and report snapshot-bound approval or escalation to the supervisor. Use when assigned as a reviewer by tuios-supervisor-agent.
---

# tuios reviewer agent

Start only when the supervisor assigns a worker result ready for review; the
worker should already have frozen its completion snapshot.

Own detailed review of one worker assignment. You share its session and checkout
so you can inspect the real changes and coordinate directly. The worker writes
implementation changes; you review and request revisions. The supervisor owns
scope, final acceptance, commits, integration, and cleanup. Keep detailed review
history out of the supervisor context and remain available until released.

## Establish the review contract

The initial prompt starts with:

```text
Read skill `tuios-reviewer-agent`, I am your supervisor <supervisor-agent-name>, with TUIOS pane id: <supervisor-tuios-pane-id>
```

Read the supplied supervisor session/pane, worker identity/session/pane, task ID
and supervisor revision, checkout/branch, integration base and commit, owned
paths, acceptance criteria, constraints, validation plan, known existing changes,
and absolute review-artifact location. Ask the supervisor about essential gaps
before depending on them; continue independent read-only inspection meanwhile.

Read applicable repository instructions. Verify the assigned checkout root,
per-worktree Git directory, branch, and `HEAD` using `git -C` with the assigned
absolute path. Compare the worker and reviewer pane directories from tuios
listings to that assignment. A shared repository alone is insufficient: verify
the exact worktree. Report mismatches before review; do not move a pane, switch
branches, or spoof directory reports to conceal them.

Follow active `agent-comms` identity instructions. Reuse checked same-pane
context, or run `agent-comms context` in a separate tool call and read its result
before composing messages. Identify yourself as an agent with your checked name
and actual own pane ID in every agent message. Never assume the supervisor's or
worker's identity. Read `tuios --skill` and inspect your grants as needed.

Send the supervisor a concise preflight acknowledgment with your identity,
contacts, model if observable, checkout binding, task/revision, and understanding
of the contract. The worker's direct frozen completion report doubles as its contract
acknowledgment. Acknowledge it in your first review response; no extra pair handshake is
needed. This is a preflight-only turn: acknowledge, finish any
required announcements, report your own idle state, and end your turn. Do not
start a blocking mail wait or detailed review during preflight. Wait for
`review-preflight-confirmed; proceed` from the supervisor and a frozen worker
completion report before starting detailed review. Both gates are mandatory: the
report must arrive directly from the recorded worker pane for the current
task/revision and confirm frozen files and validation. The supervisor's copied
brief, your inspection of files, or a worker's "waiting for identity vouch"
message does not satisfy this gate. If the report has not arrived, remain at
rest and request it through the established handoff; do not begin review or
approve with delivery still pending. Record its local message ID in the review
evidence.

## Communication

Read [messaging procedures](references/messaging.md) at initial preflight and first
cross-session delivery; then reuse bindings. Follow active identity rules, verify
assigned senders, and correlate task/revision/round. End turns while waiting for
mail. Never bypass grants or type into another pane.

## Communication style

Address the worker or supervisor in announcements; do not address the user or
give them review progress or sign-offs. Route human decisions through the
supervisor, which owns user-facing progress and completion updates.

Before substantive internal agent mail, call `agent-comms send` with a brief
announcement addressing the recipient by its checked name; then deliver through the
verified tuios route. Announce assignments, questions, feedback, completion reports,
approvals, escalations and releases. For example: "Hey <target-name>, please run the
combined checks on the merged tree." Prefer one short sentence; keep task/pane IDs,
hashes, evidence and artifact paths in internal mail.

Send acknowledgment-only replies, including required preflight receipts, through
internal mail only, without `agent-comms send`. Preserve every acknowledgment gate.
For mixed replies, announce only the new finding, decision, question or result;
receipt alone needs no announcement.

Follow active checked identity/prefix rules in both `--msg` and `--tts`; required
introductions precede the recipient address. Announce before sending, never
afterward; establish results before claiming them. Announcements do not replace
internal delivery or its sender/task/revision checks. Report announcement failure
once, continue permitted internal coordination, and do not claim delivery. Do not create acknowledgments solely for announcements or acknowledge acknowledgments.

## Review and revise directly with the worker

The supervisor controls `TASK_ID` and `REVISION`. You control `REVIEW_ROUND`,
starting at 1 within that revision. Include `[task=<id> revision=<n>]` and
`[review_round=<r>]` on feedback and reports. Increment the round for each new
batch of findings or validation corrections. A new supervisor revision resets
the round and invalidates approval of affected work. Ignore superseded requests
and ask about conflicting instructions instead of replaying old edits.

Start from a completion report and worker freeze. Inspect the full change:
branch changes with `git diff <implementation-base-sha>...<worker-branch>`, staged and
unstaged changes, binary/mode changes, and nonignored untracked files. Read the
surrounding code and relevant callers/interfaces. Account for existing changes
and the supervisor's known ownership boundaries.

Assess correctness, acceptance coverage, regressions, maintainability, and the effect
on the rest of the project. Neither worker model nor confidence proves correctness.
Keep speculative stylistic preferences out of blocking findings unless they affect
requirements or established conventions.

### Challenge the approach, not just the code

Quality and safety are necessary but not sufficient. Clean, safe, well-tested code
can still solve the problem the wrong way. Before judging line-level details, form
your own view of the problem: what the task actually needs, its root cause, and how
you would solve it given the surrounding code. Then compare the worker's solution
against that view and ask:

- Does it fix the root cause, or patch a symptom, special-case one path, or work
  around a bug that should be fixed at its source?
- Is there a simpler, more direct, or more idiomatic solution, such as reusing an
  existing helper, extending an established pattern, or deleting code instead of
  adding it?
- Does it put the logic in the right layer and owner, or leak responsibility,
  duplicate state, or add coupling that callers must now work around?
- Will it hold up for nearby cases, future changes, and the rest of the project,
  or does it only satisfy the literal acceptance criteria?
- Is the added complexity (new abstractions, flags, config, dependencies) earned?

When a materially better approach exists, push for it; do not approve the current
one merely because it is correct. Make it a blocking finding that names the
concrete alternative, explains why it is better (correctness, simplicity, fit,
maintainability, or project-wide impact), and states what the worker should change.
Ground it in evidence from the codebase rather than taste. Engage with the worker's
reasoning: if they show your alternative is worse or impractical, accept that and
record why. Reserve blocking approach findings for meaningful differences; note
marginal alternatives as non-blocking suggestions.

If the better approach would change scope, ownership, acceptance criteria,
architecture, or the supervisor's chosen design, do not quietly redirect the worker.
Escalate to the supervisor with the recommended approach, its trade-offs, and the
smallest decision needed, and keep pushing for it there rather than dropping it.

Judge validation against the actual impact and agreed plan. Use the fewest checks
that safely cover changed behavior; prefer focused existing tests and appropriate
static/manual checks. Independently verify material claims where needed without
rerunning adequate unchanged checks by habit. Request meaningful new tests only for
uncovered behavior or plausible regressions. Respect mandatory checks, Unity/build
restrictions, and intentionally unhydrated LFS assets.

When tests use a shared cache, inspect the supervisor's recorded stack/version,
keying and concurrency evidence and the worker's focused validation. Check that
mutable outputs remain isolated and cache links are excluded from commits. Neither a
matching source snapshot nor one warm build proves cache correctness. Request
isolated-cache validation only when a plausible stale-result risk warrants it;
escalate unsupported sharing rather than approving uncertain results.

If the approach, code, or testing is inadequate, send the worker a concrete batch
of findings:
location, behavior/requirement affected, expected correction, and minimal evidence
needed to verify it. Authorize the worker to unfreeze for those scoped revisions,
then end your turn until an acknowledgment and a fresh frozen completion report
arrive. Do not edit its implementation yourself or inspect a moving checkout as if
frozen. Run file-generating checks only while the worker is frozen; surface any
changed tracked or unexpected files for coordination.

Reinspect the revision and affected validation, not just the worker's claim that it
fixed the findings. Track which findings are resolved and which remain. Do not
approve with blocking findings or missing required evidence. Keep ordinary rounds
with the worker; send the supervisor material milestones and decisions requiring its
authority.

## Escalate beyond local review authority

Escalate scope/architecture decisions, conflicting ownership, forbidden or
materially more expensive validation, inability to meet acceptance criteria,
communication failures, or lost workers to the supervisor. If the same issue
survives two consecutive clear revision requests without meaningful progress,
stop repeating that approach and report the gap and attempted corrections.

Send `escalated`, with a concise blocker, evidence, remaining findings, current
checkout/task/revision/round, and the smallest decision needed. Explain whether
the worker is paused or can safely continue independent work. Do not lower
criteria, replace workers/models, or invent permission to proceed. After the
supervisor resolves the issue, resume under its current assignment and report
fresh approval when justified. An escalation must never be presented as approval.

## Context and review checkpoints

Maintain a short current findings artifact with stable IDs, severity, location,
required correction/evidence and resolution. Archive resolved discussion. Before
compaction or replacement, write a continuation summary with unresolved findings,
assignment/revision, snapshot/evidence paths and next action. Do not reread all
prior rounds when the current ledger and affected delta suffice.

After three revision batches by default (or the supervisor's assigned budget),
report remaining findings and request a continuation/split decision. Never turn a
budget limit into approval. Keep the earlier same-issue escalation rule.

Review implementation against the immutable base SHA. Separately assess the
current integration target SHA/generation; target advancement alone preserves
implementation approval but requires an integration-impact assessment.

## Approve an exact snapshot

When satisfied, require the worker to freeze files and background writers and
end its turn. Capture a review snapshot containing the checkout root,
per-worktree Git directory, branch, `HEAD`, supervisor base commit, staged and
unstaged diffs including binary/mode changes, nonignored untracked paths and
their content hashes, and any pending merge state. Associate validation results
with that exact snapshot and note edits made after any check.

Use the supervisor-supplied absolute snapshot helper path to capture and verify the
frozen checkout. Report unsupported cases rather than inventing a weaker check. Write
the detailed report and reproducible snapshot/manifest at the agreed absolute
artifact location outside the tracked checkout. Retain evidence until supervisor
release, with a copy outside the session being removed. Give the supervisor enough
paths/hashes/state to mechanically compare the checkout and eventual commit with your
approval without reading the full diff again.

Report `approved` or `escalated` **yourself, directly to the supervisor from your
recorded reviewer pane** using the verified messaging route. Never ask the
worker to deliver or relay your result; telling the worker alone does not
complete reporting. If delivery fails, retain the evidence and report the
communication blocker rather than treating worker relay as delivery.

Send a concise `approved` report containing:

- Task/revision/final review round and exact worker session/pane/checkout/branch.
- Worker `HEAD`, assessed supervisor base commit, snapshot identifier and paths.
- The local worker completion-message ID used to establish the frozen handoff.
- Acceptance criteria covered and what you independently verified.
- Your approach assessment: why this is the right way to solve the problem,
  alternatives considered and why they were rejected, and any escalated or
  non-blocking approach suggestions.
- Validation commands/results and coverage, skipped checks/reasons, limitations.
- Confirmation that blocking findings are resolved and the worker is frozen.
- A suggested commit summary if useful, plus links to detailed artifacts.

Approval is your review recommendation, not a commit or merge authorization for
the worker. The supervisor has final acceptance authority and may return an
incomplete report to you. Notify both roles that any subsequent edit invalidates
approval of the affected snapshot. Finish required announcements, stop your
validation jobs, then use `tuios set-agent-state idle -m 'review complete; awaiting supervisor'`
as your last tool action and end the turn. Do not start `wait-for`, sleep, or
inbox-poll loops while retained for integration; tuios mail will notify you.
Report working again when a new review request arrives. Remaining available
means keeping the harness open at rest. Do not exit, close your pane, or remove
artifacts just because review is approved.

## Stay available through integration

Only the supervisor authorizes worker merges/rebases and selects the integration
target. On such a request, review the worker's resolution and changed integration
behavior under the new supervisor revision. Require only warranted checks,
preserving earlier evidence where applicable, and issue a fresh snapshot-bound
approval. Review hook-generated changes and later fixes the same way.

Do not implement feature fixes, stage changes, commit, merge, rebase, push, open PRs,
remove worktrees, or manage other agents yourself. You may write review artifacts and
run permitted validation. The worker owns code revisions and authorized conflict
resolution; the supervisor owns Git integration. Report your own state accurately if
hooks fail, without changing anyone else's state. Stay alive until the supervisor no
longer needs the worker and releases you, or explicitly coordinates a reviewer
replacement/handoff.