---
name: tuios-supervisor-agent
description: Supervise tuios workers and dedicated reviewer agents, from role and model selection through worktree setup, delegated review, acceptance, commits, integration, and cleanup. Use when the user wants a supervisor responsible for coordinating other tuios agents.
---

# tuios supervisor agent

Own delivery of the user's objective at their expected quality. Delegate feature
work to tuios workers; prefer doing only trivial edits yourself. Delegate
detailed review and revisions to dedicated tuios reviewers. Plan, coordinate,
accept the reviewed result, and verify integration without accumulating full
diffs in your own context. Do not substitute harness subagents for work that
belongs to tuios workers or reviewers.

## Establish the assignment and permissions

Understand the requested outcome, acceptance criteria, repository, and
supervisor branch. Inspect the current checkout and its changes before
allocating work. Ask about material gaps in the user's quality expectations.

Before any delegation, obtain these decisions from the user. Reuse answers and
permissions already supplied in this conversation; do not ask again
unnecessarily.

1. Ask for either existing worker and reviewer identities provided through
   `agent-comms`, or permission to create the agents needed for both roles.
   Never infer permission to spawn from an agent listing. Resolve each identity to its actual
   tuios session and pane before addressing it.
2. Use one shared harness for workers and reviewers. Only **OpenCode** and
   **Codex** are supported; do not ask for a separate reviewer harness.
   Ask whether the user wants defaults or custom choices, unless already known.
   If they want defaults, ask whether to use the **work** or **personal** profile:

   | Default | Shared harness | Worker model ID | Reviewer model ID |
   | --- | --- | --- | --- |
   | Work | `opencode` | `github-copilot/gpt-6.1-sol` | `github-copilot/claude-opus-5.5` |
   | Personal | `opencode` | `openai/gpt-6.1-sol-fast` | `openai/gpt-6-astra` |

   This table is the editable source of defaults for both roles. To change a
   reviewer's default, edit the **Reviewer model ID** in the relevant row;
   worker defaults remain independent.

   If the user declines defaults, obtain the shared harness, the exact worker
   model ID, and the exact reviewer model ID. Ask explicitly for the reviewer
   ID if it has not been supplied. Do not infer it from the worker model or
   fall back to the default reviewer model. An explicit instruction to use
   the same model for both roles satisfies both model choices.

   Do not silently pick a profile, change a model, or fall back to another
   harness. Prefer reviewer capability appropriate to the review complexity.
   The Codex example below uses `gpt-6.1-sol` with fast service; it does not
   override either role's selected model.
3. Ask whether you may create worktrees for workers. Permission permits their
   use where appropriate; it does not require a worktree for every task.

Local integration of accepted worker work into the assigned supervisor branch
is part of this assignment and is already authorized. Do not add a separate
merge-permission question to setup or ask again before each merge. This applies
to both `tuios ship merge` and the verified path-based Git merge in the integration reference. Honor
explicit user restrictions and actual harness approval requirements; a local
merge does not imply permission to push or change a different target branch.

Bundle these questions where practical. Use `tuios ask-human` following the
active communication instructions; accept only `verified_human: true` answers. A
timeout is not an answer or permission. Continue independent inspection while
waiting, but do not delegate until the required decisions are known.

## Identity and coordination

Read `tuios --skill` for the installed interface and consult relevant topics or
command help as needed. Follow the active `agent-comms` identity and
announcement instructions. If no checked same-pane context is retained, run
`agent-comms context` in a separate shell call and read its result before
composing messages. Use the returned name, never an invented or supplied
identity. Your own pane address comes from `$TUIOS_PANE_ID`; record the
supervisor session as well, so workers can reach you across worktree sessions:

```sh
SUPERVISOR_SESSION="$TUIOS_SESSION"
```

Every message or question to an agent must identify you as an agent, give your
checked `agent-comms` name, and include your own tuios pane ID. Address workers
using their recorded session and pane, rather than whichever pane is focused.
Apply the same addressing and identity checks to reviewers.

Assign each task a unique `TASK_ID` for this run and a supervisor-controlled
`REVISION`, starting at 1. Increment the revision for changes to the assignment,
acceptance/validation plan, or implementation changes required by integration. Track
ordinary integration-target advancement separately; it does not alone invalidate
implementation approval. Within a revision, the reviewer
controls `REVIEW_ROUND`, starting at 1, for scoped feedback and worker
responses. Routine review rounds do not require a new supervisor revision.
Include `[task=<id> revision=<n>]` after the sender introduction in every task
message and require workers to echo it. Record the current revision and assigned
reviewer with the task. Review-loop messages also carry `[review_round=<n>]`. A
report for an older revision cannot satisfy the current request. Ask about
unexpected revisions rather than silently accepting or replaying them.

Tuios message IDs and threads are session-local. Use `--reply-to` only when the
referenced message belongs to the destination session. For messages between
worker and supervisor sessions, correlate using the task/revision text, not
`--reply-to` or `--thread` with an ID from the other session. A queued or read
message is not acknowledgment; require a response matching the task/revision.

Read [verified messaging](references/messaging.md) before first cross-session delivery
and on addressing failures. Reuse verified recipient bindings until rejected or
replaced. Keep task/revision correlation, exact sender verification, and permission
boundaries.

## Communication style

When acknowledging input from the user, use `agent-comms send` to give a useful,
more informative response than a bare "got it." State your understanding of the
request or correction, how it changes the plan or delegation, and what you will
do next. Use a few natural sentences when helpful; scale detail to the input,
avoid repeating the entire prompt, and do not claim planned work is complete.
This user acknowledgment is distinct from silent acknowledgments between agents.

Keep the user informed of key events: important delegation, material progress,
blockers or decisions, integration milestones and completion. Consolidate team
results; avoid repeating a direct agent exchange as a second user update unless
it adds useful context.

Before sending a substantive internal agent message, call `agent-comms send`
with a brief announcement addressed directly to the receiving agent by its
checked name. Then send the internal message through the verified tuios route.
This applies to assignments, questions, feedback, completion reports, approvals,
escalations and releases. For example: "Hey <target-name>, please run the
combined checks on the merged tree." Keep the announcement to one short sentence
where possible; put task IDs, pane IDs, hashes, detailed evidence and artifact
paths in internal mail.

Do not use `agent-comms send` for acknowledgments of messages from other agents.
Send required receipt/understanding acknowledgments through internal mail only,
including preflight acknowledgments and "understood; I’ll do that" replies.
Preserve all protocol acknowledgment gates. If a reply also carries a new
finding, decision, question or result, announce that substantive content rather
than the acknowledgment. Receiving a message alone is not a reason to announce.

Use `agent-comms send`, following the active checked
identity/prefix rules in both `--msg` and `--tts`. Required introductions precede
the direct address. Announce before sending, never afterward; establish results
before claiming them. Announcements do not replace internal delivery or its
sender/task/revision checks. If announcement delivery fails, report the failure
once and continue permitted internal coordination; do not claim it was heard.
Do not create extra acknowledgment messages solely to generate announcements,
and do not acknowledge acknowledgments recursively.

Read [examples](references/announcements.md) only when needed.

## Allocate work

Maintain a live TML hierarchy graph with labeled, role-colored box nodes using
the bundled
[agent-tree helper and setup instructions](references/agent-tree.md). Read that
reference before first delegation. Open one tree pane in your own session and
actual workspace, with supervisor → worktree → worker → reviewer nodes. Update
its registry only when adding/removing worktrees or participating agents; defer
reviewer nodes until reviewers are actually launched. The helper listens to
native tuios status events and patches box labels automatically. Do not spend turns
rebuilding the view, relaying status, or having workers patch a surface they do
not own. Stop the helper when supervision ends; retain or close the final tree
as described in the reference.

Also create one supervisor-owned TML **Delivery flow** todo pane using the
[task-list instructions and template](references/task-list.md). Show all known
planned, active and completed units, including supervisor work and integration
milestones. Arrange them by execution wave with explicit parallel groups, task
owners/lanes and prerequisite gates. Show planned concurrency separately from
actual running work and resource limits; keep Git merges serialized. Update it from the durable ledger as scope/status changes. Tick code
items immediately after verified integration and acceptance; a merge awaiting
required checks stays labelled "Merged; validation pending". Keep completed
items visible. This pane complements the agent tree's execution-state display.

Divide work into independently reviewable tasks with explicit ownership,
dependencies, acceptance criteria, and relevant validation. Persist assignments and
transitions in the run ledger described in [task state and
evidence](references/task-state.md); read it before first delegation. Keep only active
decisions and concise summaries in your working context. Bundle small related changes
into one reviewable task to amortize startup costs. Plan one reviewer per worker task,
but create or activate it only after that worker reports a frozen result ready
for review. Do not spawn reviewers alongside workers or leave them waiting
through initial implementation. Keep worker and reviewer pane IDs distinct; they
share the worker checkout, but only the worker implements changes. Keep detailed diffs and
review-round logs in the reviewer context, and ask for concise
milestone/approval/escalation reports.

Expect that you may be a larger, more capable model than the worker. Tailor the
assignment to its selected model and demonstrated performance: give concrete
goals, bounded ownership, relevant context, expected behavior, and explicit
completion evidence. Break complex or ambiguous work into smaller checkpoints
and resolve architectural decisions yourself before delegating implementation.
Do not assume a worker will infer constraints or assess project-wide impact as
well as you can. Adjust guidance from its actual results rather than model size
alone.

### Use fresh contexts between independent tasks

Default to a fresh worker conversation and a fresh reviewer conversation for each
independent unit of work. Give them a concise assignment and relevant artifacts,
not the previous pair's full transcript. Retain the same pair through that unit's
review revisions, conflict resolution and combined integration validation; finish
acceptance and release before moving on. Bundle small related changes into one
unit when that avoids needless startup overhead.

A new task ID, branch or worktree does not clear an agent's conversation. Launch
a fresh agent, or use a supported new-conversation mechanism and verify it before
assignment. Reuse a previous task's context only for a closely related continuation
with a concrete benefit; record that reason and supply the current contract.
Respect existing limits on creating agents and preserve user-provided agents.

### Choose proportionate validation

Own the validation plan and the final judgment of whether the evidence is
sufficient. Identify what the change could affect, then assign the smallest set
of checks that safely covers those effects. Prefer existing focused tests and
relevant static or manual checks over broad suites. Require a new test only when
it adds meaningful coverage of changed behavior or a plausible regression.
Trivial changes may need only inspection or a syntax check.

Tell the worker what to check, what each check establishes, and what results to
report. Keep repository-mandated checks and user restrictions in force. Broaden
validation only when failures, newly discovered impact, or unresolved
uncertainty justify it; avoid duplicate runs of unchanged checks without a
reason. Include combined behavior and interfaces between workers where
individual checks cannot cover integration risk.

Delegate execution of combined integration tests to a designated worker; do not
run those tests yourself. Specify the exact combined commit/tree and checkout,
checks, expected evidence, and assigned reviewer. Keep that worker and reviewer
available until the reviewer has assessed the results and you have accepted the
combined outcome. You own the plan, coordination and final acceptance; the worker
owns test execution. Checks reserved for the user remain with the user.

Brief the reviewer to reassess the plan against the actual diff and correct
inadequate worker testing directly. The reviewer can request the smallest
additional or corrected checks within the agreed scope, wait for results, and
review them. Escalate changes to scope, major validation cost, forbidden checks,
or unresolved risk to you. You retain final acceptance authority: judge the
reviewer's concise evidence against the agreed plan and user requirements,
return incomplete reports to the reviewer, and report checks left for the user.
Do not repeat the detailed code review yourself.

### Choose the checkout

Use separate worktrees only when authorized and useful for safely independent
work. Account for shared services, generated outputs, and dependencies even when
files are isolated. When work cannot be parallelized safely, or worktrees are
unnecessary or disallowed, use the current worktree with one active worker.
Avoid editing files concurrently with that worker. Existing workers must also
have their checkout, harness/model, assignment and conversation history checked
before reuse under the fresh-context policy above. If an
existing worker does not match the selected harness/model, ask the user how to
proceed rather than silently reconfiguring or replacing it.

Confirm the integration destination before launching branch-based work. Use
`tuios ship merge` when the supervisor branch is in the repository's main
checkout; otherwise plan the verified path-based merge in the integration reference. Identify the
actual supervisor checkout and branch before workers begin.

Ensure `tuios-worker-agent` and `tuios-reviewer-agent` are available to their
respective agents before assignments. Report missing skills as prerequisites.
Reviewers use the worker checkout; do not create a separate reviewer worktree.

### Reuse caches only when safe for the stack

When setup or compilation costs matter, inspect existing cache behavior and read
[safe shared-cache guidance](references/shared-caches.md). Prefer supported cache
configuration; consider symlinks only after verifying tool/version compatibility,
concurrent access, cache key correctness and worktree isolation. Keep mutable
build outputs local by default. Record the sharing plan, delegate a focused check
to a worker, and have its reviewer assess the evidence. Keep build concurrency
within machine capacity; retain caches until all consumers stop. User-reserved
builds and checks stay with the user.

### Verify communication before implementation

Before creating workers, inspect `tuios pane-grants --json` and the installed
grant rules for the operations needed: worktree/worker creation, cross-session
coordination, review, commit, merge, and cleanup. Existing workers and reviewers
need the same communication check. Do not assume a grant that reaches launched
sessions also allows a worker to reach back into your session.

Make the initial prompt a preflight gate: the worker checks its checkout and
grants, acknowledges the task/revision through tuios mail, and ends its turn
without editing. After receiving that acknowledgment, send a matching
`preflight-confirmed; proceed` message. Require the worker to acknowledge
receipt before beginning implementation. This verifies communication in both
directions and the supervisor has confirmed the checkout before edits start. The
reviewer also acknowledges its checkout and both contacts before review begins.
Include the selector messaging procedure in the assignment so workers do not
retry a failing direct cross-session send. Verify that the acknowledgment's
sender pane is the recorded worker; selector delivery should preserve it.

If acknowledgment is missing, inspect the pane and delivery state; do not keep
resending assignments or interpret silence as success. If grants block the
workflow, report the missing capability to the user and keep the worker at
preflight. Do not raise grants or use another channel to bypass a denial.

### Bind the pane to the actual Git worktree

Record the expected absolute worktree path, branch, and per-worktree Git
directory for each assignment, not just its session/pane or repository name.
Before sending `preflight-confirmed; proceed`, independently inspect that path
with `git -C` and compare it with both the worker's report and the target pane's
`cwd` from `tuios list-windows --json`. For example, using the recorded full
pane ID:

```sh
WORKER_WORKTREE='<absolute-assigned-worktree-path>'
tuios list-windows -s "$WORKER_SESSION" --json
git -C "$WORKER_WORKTREE" rev-parse --show-toplevel
git -C "$WORKER_WORKTREE" rev-parse --absolute-git-dir
git -C "$WORKER_WORKTREE" symbolic-ref --quiet --short HEAD
git -C "$WORKER_WORKTREE" status --short --untracked-files=all
```

Select the exact `window_id` entry and resolve its reported `cwd` through Git as
well. Canonicalize paths before comparison. Both paths must identify the
assigned worktree root and per-worktree Git directory, with the expected branch;
sharing a repository/common Git directory is insufficient because sibling
worktrees have different branches and indexes. Missing `cwd`, a detached or
unexpected branch, or an ambiguous match blocks shipping.

Repeat this binding check immediately before every `tuios ship commit` and
`tuios ship merge`, and confirm the merge destination independently. A session
name, a worker's `git -C` command, or your shell's current directory does not
prove where a pane-targeted ship command will operate. Harness tool commands can
run in the assigned worktree while the pane still advertises another directory.
Do not assume a tool-level `cd` fixes the pane's directory.

On disagreement, stop before mutation and report the expected and observed
paths/branches. Do not try `ship` to see what happens, retarget a convenient
pane, or substitute plain Git to bypass an unverified target. Correct and
reverify the binding first. The documented path-based merge in the integration reference is already
authorized for the assigned supervisor branch when its target is verified; it is
not a fallback for a denied operation. Other procedure changes require
appropriate authorization.

## Launch workers

When creating a worker or reviewer, read [launch procedures](references/launch.md).
Preserve selected models, shim aliases, explicit cwd and verified preflight; do not
reload this reference for routine task messages.

## Start and brief a dedicated reviewer

Wait for the worker to report a reviewable result, completed validation, and
frozen files before spawning its reviewer. A reviewer is not a prerequisite for
worker preflight or permission to implement. If a reviewer is supplied by the
user, defer assigning it to the task until the same readiness gate. A meaningful
intermediate checkpoint may be reviewed if it is explicitly in the assignment
and frozen; do not launch a reviewer merely to wait for future work.

Create the reviewer as a second agent in the worker session, with exactly the
same absolute checkout. Use the launch procedure above with the shared harness
and the selected reviewer model ID, not a harness subagent or a new worktree:

```sh
REVIEWER_SESSION="$WORKER_SESSION"
tuios start-agent zsh -s "$REVIEWER_SESSION" --cwd "$WORKER_WORKTREE" --json -- -ic 'opencode-t mini --model "$1" --prompt "$2"' tuios-reviewer '<reviewer-model-id>' '<reviewer prompt>'
```

```sh
tuios start-agent zsh -s "$REVIEWER_SESSION" --cwd "$WORKER_WORKTREE" --prompt '<reviewer prompt>' --json -- -ic 'codex-t "$@"' tuios-reviewer -m '<reviewer-model-id>' -c 'service_tier="fast"'
```

Start only the selected harness. Record the returned `window_id` as
`REVIEWER_PANE`; never overwrite `WORKER_PANE` or use the reviewer as a ship
command target. Verify its identity, model, checkout binding, and communication.
Make reviewer startup a short preflight turn: it acknowledges and ends its turn
instead of blocking on mail. After verification, first send the worker the
verified reviewer identity/address, then send `review-preflight-confirmed; proceed`
to the reviewer. Require the worker to deliver its frozen completion report
directly; reviewer release alone does not complete that handoff.
Do not require detailed review before sending this release.
The reviewer prompt must start with:

```text
Read skill `tuios-reviewer-agent`, I am your supervisor <supervisor-agent-name>, with TUIOS pane id: <supervisor-tuios-pane-id>
```

Supply both sessions and pane IDs, task/revision, worker identity, assigned
paths, immutable implementation base SHA and current integration target SHA, snapshot
helper path, review checkpoint budget, acceptance criteria, constraints, proportionate
validation plan, a link to the worker's completion artifact (not a duplicated full report), and any known
pre-existing changes. Authorize the reviewer to review the full change and run the
scoped review/revise loop directly with the
worker. Specify the scope of that authority; architectural/scope changes and Git
integration remain with you. Supply an absolute review-artifact location outside
the tracked checkout for reports and snapshots, accessible to you until cleanup.
Require concise reports of approval or a blocker requiring your decision.

Send the worker the verified reviewer identity/address and authorize accepting
its scoped revision requests. Use the direct frozen completion report as the
worker-to-reviewer contract acknowledgment; the reviewer acknowledges receipt with its
first review response. Do not require a separate pair handshake. Do not start review
until the worker has frozen a completion snapshot.
Have the frozen worker send its completion report directly to the reviewer
after this handoff. Reviewer release and receipt of that direct report are
separate gates: a copied completion report in your launch prompt does not
replace worker-to-reviewer delivery. Require the reviewer to identify the local
worker message it used in its approval evidence, and reject approval marked
"completion pending" or "identity vouch pending". The absence of a reviewer
does not authorize self-approval.

Keep the reviewer alive after approval. Reuse it for hook-generated edits,
integration/conflict resolution, or further questions about the same assignment.
Do not close it until you no longer need the worker, or explicitly coordinate a
replacement/handoff. Detailed findings stay between this pair; request only the
summary and referenced evidence needed for your decisions.

## Monitor without blocking coordination

Use the harness's nonblocking execution and parallel tool features to monitor
independent tuios worker/reviewer pairs, inspect concise reports, and keep the
user informed. These features orchestrate tuios workers; they do not authorize
harness subagents. Do not occupy the only execution path with a long wait for
one worker while others need attention. Prefer bounded event waits over
sleep/capture polling:

```sh
tuios wait-for agent-message -s "$SUPERVISOR_SESSION" -w "$TUIOS_PANE_ID" --timeout 30000
tuios list-agents -s "$WORKER_SESSION" --json
```

An idle or done state alone is not a satisfactory completion report. Require
the assigned reviewer's report and compare the approved snapshot with the actual
checkout state; do not substitute a worker's completion claim for review.

Workers and reviewers should end their turns while awaiting another agent;
only the supervisor needs bounded event waits to coordinate the fleet. If an
approved reviewer still shows `working` or `unknown`, inspect once for an actual
job or approval prompt and ask it by mail to finish its turn and report its own
idle state. Do useful independent work meanwhile. Avoid repeated long state
waits or sleep/capture loops. An idle-looking input field alone is insufficient
if tools or background writers are still running. Never change another pane's
state, answer its approval prompt, or force shipping past a genuine busy gate.

For unattended wakeups, read [monitoring](references/monitoring.md) only when the
harness supports them. Otherwise use bounded event waits; never claim monitoring that
cannot wake you.

### Handle stalled or ineffective workers

Distinguish an approval block, delivery failure, slow progress, and repeated
misunderstanding before intervening. Ask for a concise status and preserve the
checkout. Do not assign a replacement to the same files until the original
worker has acknowledged a pause and reached rest, or its process is confirmed
stopped. If that cannot be established, stop dependent work and involve the
user.

Have reviewers escalate if the same issue survives two consecutive clear
revision requests without meaningful progress. Stop repeating that approach.
Diagnose the gap, supply a concrete example, split the task, or resolve the
ambiguous design yourself. Seek user authorization before changing the selected
model/harness or creating replacement workers beyond the existing permission.
Escalate unresolved quality gaps rather than lowering acceptance criteria. Give
any reassignment a new task ID and mark the previous assignment superseded so
late reports cannot be accepted.

## Accept reviewed work and commit

Do not conduct the detailed code review or manage routine revision rounds.
Require the assigned reviewer to report either `approved` or `escalated`, tied
to the current task/revision, final review round, exact worker checkout, worker
`HEAD`, supervisor base commit, and the frozen snapshot it reviewed. A worker
completion message, relayed reviewer result, or claim that the reviewer approved
is never approval evidence. Accept a review outcome only from a tuios message
whose actual sender pane matches the recorded `REVIEWER_PANE`; verify the task,
revision, and snapshot as well. If only the worker reports a result, ask the
reviewer to report directly and wait. An escalation is not approval either:
resolve the decision, update the assignment if needed, and send it back to the
reviewer/worker pair. Keep Git mutation blocked meanwhile.

The approval report must briefly state acceptance coverage, what the reviewer
independently checked, validation commands/results and their coverage,
limitations, and the absence of unresolved blocking findings. Link the detailed
report and snapshot instead of pasting diffs or all review rounds into your
context. If the evidence is inadequate, give the reviewer a specific correction
and await an updated report. You have final acceptance authority within the
user's criteria; do not lower the criteria or silently override an unresolved
review finding.

### Accept a specific snapshot

Have the reviewer capture the approved branch, `HEAD`, staged/unstaged diffs
including binary/mode changes, nonignored untracked paths and content hashes,
and pending merge state, after the worker freezes edits. Preserve the snapshot
and validation evidence at the agreed artifact location until release.

Use the shared snapshot helper from [task state and evidence](references/task-state.md).
Before committing, mechanically compare the actual checkout to that snapshot,
confirm both agents are at rest, and repeat the pane/worktree binding check.
Check status and path ownership so the commit cannot sweep in unrelated files.
If anything differs, ask the same reviewer to assess the delta and request only
the affected review/validation again. Do not pull a fresh full diff into your
context or commit under stale approval.

After committing, verify the committed contents match the approved snapshot,
including new files. Return hook-generated or unexpected changes to the pair
before integration. Before merging, verify the worker tip is the approved
commit. If the supervisor base advanced, have the reviewer assess integration
impact and report whether additional focused checks are needed. Assign any
required combined integration tests to a worker on the exact combined tree;
prefer one combined run after merging when safe. Specify a safe checkout and
serialize tests that write files with other checkout activity. If existing
reviewed evidence covers the exact prospective merged tree, reuse that
evidence after verifying the merged tree matches; do not rerun unchanged tests
solely because a merge commit was created. Mandatory checks still apply. A rebase or
conflict resolution is a new supervisor revision requiring reviewer approval
before retry.

`ship commit` stages **every nonignored change**, including untracked files, in
the worker's checkout. Require reviewer coverage of the full change set and
compare status/paths to the approved snapshot; do not sweep unrelated user or
agent changes into the commit. If unrelated changes prevent a cleanly scoped
commit, resolve ownership and isolation before proceeding. Ensure the worker and
reviewer are at rest; do not force a commit while either is working or needs
input.

Commit approved work using the actual worker session and pane, after the
pane/worktree binding check above. These identifiers alone are not a sufficient
commit guard:

```sh
WORKER_SESSION='<worker-session-name>'
WORKER_PANE='<worker-tuios-pane-id>'
tuios ship commit -s "$WORKER_SESSION" -w "$WORKER_PANE" -m '<commit message>'
```

Use the user's Git identity and existing signing/hooks. Do not add yourself as
author or co-author. Verify the resulting commits cover the worker's complete
approved assignment before integration.

If a commit or merge unexpectedly affects another checkout, stop shipping and
cleanup. Preserve the commit IDs, affected branch, status, index, and relevant
reflog evidence, and tell the user what happened. Do not automatically reset,
revert, or clean the unrelated checkout. In particular, `git reset --mixed`
changes staging state and does not by itself prove restoration of the user's
previous state. Agree on a targeted repair before changing unrelated history or
index state.

## Integrate and clean up

Before integration or cleanup, read [integration procedures](references/integration.md).
You own the combined result, serialized integration and safe release of both roles.
