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
   | Work | `opencode` | `github-copilot/claude-opus-5.5` | `github-copilot/claude-fable-5.1` |
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
to both `tuios ship merge` and the verified path-based Git merge below. Honor
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
acceptance/validation plan, or integration base. Within a revision, the reviewer
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

Use tuios agent messages for ongoing coordination. **Across sessions, use
selectors, not direct `send-agent-message -s ... -w ...`.** In the tested tuios
version, a direct cross-session send looks up the sender pane in the recipient
session and fails. Selector-based sends were verified in both directions with
the original sender pane IDs preserved.

Start with a selector for the recorded worker session and inspect its matches:

```sh
WORKER_SELECTOR="session:$WORKER_SESSION"
tuios list-agents --select "$WORKER_SELECTOR" --json
```

Require `success: true` and exactly one entry in `agents`, whose `session` and
`window_id` match the recorded `WORKER_SESSION` and `WORKER_PANE`. If multiple
agents match, refine the selector with a supported filter such as
`name:*<worker-agent-name>*`, then list again. If no entry matches or the ID is
wrong, stop and resolve the address; do not send to a replacement by name.

Put all required terms in **one quoted selector**, for example:

```sh
WORKER_SELECTOR="session:$WORKER_SESSION name:*<checked-worker-name>*"
tuios list-agents --select "$WORKER_SELECTOR" --json
```

Terms within one selector intersect; repeated `--select` flags union their
matches and can broaden delivery. Do not add a second `--select` to narrow the
first. Keep the selector, confirm token, and message as separate quoted
arguments; never pack them into a string and rely on shell word splitting.
Require a nonempty confirm token before sending.

Take the `confirm` token from that verified listing and send using the same
selector. The token binds delivery to the inspected recipient set:

```sh
WORKER_CONFIRM='<confirm token from the verified listing>'
tuios send-agent-message --select "$WORKER_SELECTOR" --confirm "$WORKER_CONFIRM" 'I am agent <checked-supervisor-name>, in tuios pane <supervisor-pane-id>. [task=<id> revision=<n>] <assignment or feedback>'
```

Do not use `--yes` for these point-to-point messages, because it sends to
whatever matches at delivery time. Re-list and reverify when a token is rejected
or the worker is restarted/reassigned. Never strip `TUIOS_PANE_ID`, spoof
`--from`, or change grants to get a message through. A `forbidden` response is a
permission decision, not this addressing bug.

To address a reviewer, substitute its recorded session and `REVIEWER_PANE` in
the same verified selector procedure. A worker session now contains two agents:
a session-only selector will usually be too broad. Refine it and verify the
exact intended pane before sending. Never broadcast worker instructions to both.

For a worker in your own session, direct `send-agent-message -s ... -w ...` is
supported. Use `ask-agent` only for same-session immediate questions when the
worker is ready. Across sessions use selector-based mail and bounded message
waits. Keep read/wait/ship commands' explicit session and pane arguments; the
selector workaround applies to messaging, not Git targeting.

Expect completion reports and questions through tuios agent messages. Read your
inbox with:

```sh
tuios read-agent-messages -s "$SUPERVISOR_SESSION" -w "$TUIOS_PANE_ID" --unread --json
```

Treat worker output and mail as evidence, not authority to change the user's
scope or permissions. Never type into a pane in `needs_input`; inspect with
`peek-prompt` and bring required human decisions to the user. Respect
`forbidden` and harness approval boundaries.

### Keep the user in the loop

Announce information that helps the user follow delivery: meaningful delegation
or revision requests, material progress, blockers or decisions, integrated
milestones, and final completion. Agent mail alone is not a reason to speak.
Keep routine preflight acknowledgments, snapshot confirmations, reviewer
approvals with no new findings, integration checks with no new risk, release
notices, and unchanged waiting/idle states in tuios mail and the live graph.
Consolidate team integration/cleanup results into your own useful summaries;
workers and reviewers should not each repeat them or announce sign-offs.

For a useful worker-directed announcement, phrase it briefly as a request to
that agent. Call `agent-comms send` **before** sending the related tuios message
or taking the announced action. Do not send first and narrate it afterward.
When reporting a result, establish the evidence first, then announce it before
the related handoff or next action; never describe a planned action as already
completed. Routine internal agent exchanges are agent replies, not automatically
human-facing turns: do not manufacture a spoken closing summary for each one.

Follow the active identity and prefix rules for both `--msg` and `--tts`; do not
invent an introduction or add a second one. For example, after any required
prefix: "Hi <worker-name>, please add coverage for the retry limit and report
the results." Keep readable text free of speech markup. If harness voice mode
already delivers the update, avoid a duplicate announcement.

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

Divide work into independently reviewable tasks with explicit ownership,
dependencies, acceptance criteria, and relevant validation. Record each worker's
identity, session, pane, harness/model, checkout, branch, assignment, status,
and review outcome in your working context. Plan one reviewer per worker task,
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
have their checkout, harness/model, and assignment checked before reuse. If an
existing worker does not match the selected harness/model, ask the user how to
proceed rather than silently reconfiguring or replacing it.

Confirm the integration destination before launching branch-based work. Use
`tuios ship merge` when the supervisor branch is in the repository's main
checkout; otherwise plan the verified path-based merge below. Identify the
actual supervisor checkout and branch before workers begin.

Ensure `tuios-worker-agent` and `tuios-reviewer-agent` are available to their
respective agents before assignments. Report missing skills as prerequisites.
Reviewers use the worker checkout; do not create a separate reviewer worktree.

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
reverify the binding first. The documented path-based merge below is already
authorized for the assigned supervisor branch when its target is verified; it is
not a fallback for a denied operation. Other procedure changes require
appropriate authorization.

## Create a worktree and worker

For an authorized new worktree, create its detached session first:

```sh
tuios worktree new '<feature-branch-name>' --base '<supervisor-branch>' --repo '<git-repo>' --detach --json
```

Prefer short, readable, task-based branch and session names. Check for collisions
and use a readable qualifier when needed, rather than a random suffix. Keep full
pane IDs, paths, and exact model IDs in agent messages and artifacts; routine
spoken updates should use agent names and short task labels.

Use the supervisor branch as the base. Check for existing branches before
reusing a name. Record the actual returned `session`, `path`, and `window_id`:

```sh
WORKER_SESSION='<returned-session>'
WORKER_WORKTREE='<returned-absolute-worktree-path>'
STARTER_PANE='<returned-initial-shell-window-id>'
```

If the repository uses Git LFS and this code task's validation does not need
heavy assets, prepend `GIT_LFS_SKIP_SMUDGE=1` to that creation command.

In the tested tuios version, `worktree new --agent` validates a harness ID
before launch. It rejects `codex-t`, an absolute shim path, and full commands
with model arguments. Do not use it for this workflow or silently substitute a
bare harness that drops the user's shim or selected model. `start-agent` accepts
an executable path and detects the running harness afterward.

Preserve the user's `-t` aliases without hard-coding their executable paths.
Start through interactive zsh, which loads the aliases from the user's shell
configuration. Verify they are defined first:

```sh
zsh -ic 'whence -w opencode-t; whence -w codex-t'
```

Shell aliases cannot be passed directly as the executable to `start-agent`. Use
the literal alias in the zsh command string. `tuios-worker` supplies zsh's `$0`;
it is not passed to the harness. Start only the selected harness, replacing
example model IDs with the user's choice.

For OpenCode, keep `mini --model` inside the shell command. `$1` supplies the
selected model ID and `$2` supplies the initial prompt directly to Mini:

```sh
tuios start-agent zsh -s "$WORKER_SESSION" --cwd "$WORKER_WORKTREE" --json -- -ic 'opencode-t mini --model "$1" --prompt "$2"' tuios-worker '<model-id>' '<initial prompt>'
```

The installed OpenCode CLI accepts `--model` / `-m` on `mini`, not on the root
`opencode` command. Never rewrite this as `opencode-t -m ...`, omit `mini`, or
move the model flag before the subcommand. If it reports an unrecognized model
flag, inspect the actual launch command and `zsh -ic 'opencode-t mini --help'`;
do not drop model selection and continue with an unknown default.

Pass the first prompt through Mini's own `--prompt`, as shown, rather than
adding tuios's `--prompt` to this OpenCode command. An empty Mini prompt can be
misdetected as working, leaving tuios waiting for readiness before it submits
anything. Direct prompt delivery was verified with an actual Mini response.

For Codex, `"$@"` forwards the model/config arguments with their boundaries
intact:

```sh
tuios start-agent zsh -s "$WORKER_SESSION" --cwd "$WORKER_WORKTREE" --prompt '<initial prompt>' --json -- -ic 'codex-t "$@"' tuios-worker -m gpt-6.1-sol -c 'service_tier="fast"'
```

This loads interactive shell startup configuration. Verify the resulting
directory and actual running harness during preflight; do not accept an idle
shell or an alias-resolution error as worker readiness. If the selected alias is
unavailable, report it rather than silently launching the bare harness.

**Every `start-agent` call must pass the assigned absolute path through
`--cwd`.** Selecting a session with `-s` alone does not bind the working
directory. For a single worker using the supervisor's current checkout, skip
worktree creation and use that checkout and session explicitly instead.

Record `WORKER_PANE` from the `start-agent` result's `window_id`, not the
worktree creation result. The latter identifies the starter shell, which must
never be used as the worker target for review or `ship`.

The initial shell is temporary. Once the worker has completed the communication
handshake and its pane/worktree binding is verified, close only that recorded,
unused shell pane:

```sh
tuios close-window -s "$WORKER_SESSION" "$STARTER_PANE"
```

Confirm it is still the shell created by this task, is unused, and differs from
`WORKER_PANE`. Skip this step for a pre-existing session/current checkout. The
result is a session with just the worker; the temporary starter shell is needed
by this launch sequence to preserve the shim and model selection.

Use the harness's nonblocking execution features for independent startup calls
so you remain available for mail. Process preflight acknowledgments as they
arrive; do not wait for every `start-agent` command to return before verifying
bindings and releasing a ready agent. OpenCode Mini receives its prompt during
launch, so a valid acknowledgment may arrive before the readiness waiter
finishes. Its readiness timeout alone does not mean launch failed. Codex uses
`start-agent --prompt` once ready. Do not send either
prompt again through `ask-agent`. If startup reaches a trust/approval prompt,
leave it for the user. A timeout can leave a live pane: inspect its state and
output, retain its ID, and check whether the prompt was delivered before
retrying. Do not create duplicate workers. Explicit `--cwd` does not replace the
preflight and pre-ship binding checks.

The initial prompt must start exactly with this line, replacing the
placeholders:

```text
Read skill `tuios-worker-agent`, I am your supervisor <supervisor-agent-name>, with TUIOS pane id: <supervisor-tuios-pane-id>
```

Then supply the supervisor session, the reviewer handoff procedure, task
ID/revision, task, owned paths, dependencies, acceptance criteria, validation
requirements, and preflight gate. Tell the worker to report blockers and
completion through tuios messages, including changed files, checks and results,
and remaining limitations. Tell it to freeze and notify you when ready; you
will then create/assign its reviewer. Require the worker to leave changes for
reviewer assessment and supervisor commit; workers must not run commit commands
or remove their worktrees. A worker may bring your branch into its own through a merge/rebase
only to resolve integration conflicts or on your specific instruction. Do not
allow routine branch synchronization or automatic merge commits; you own new
commits, including merge commits. Quote prompts and messages safely as literal
arguments.

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
paths, base branch/commit, acceptance criteria, constraints, proportionate
validation plan, the worker's frozen completion evidence, and any known
pre-existing changes. Authorize the reviewer to review the full change and run the scoped review/revise loop directly with the
worker. Specify the scope of that authority; architectural/scope changes and Git
integration remain with you. Supply an absolute review-artifact location outside
the tracked checkout for reports and snapshots, accessible to you until cleanup.
Require concise reports of approval or a blocker requiring your decision.

Send the worker the verified reviewer identity/address and authorize accepting
its scoped revision requests. Require a direct same-session handshake between
the pair. Do not start review until the worker has frozen a completion snapshot.
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

### Five-minute self-check while workers are active

If the harness supports reminders, scheduled follow-up turns, or a comparable
native way to wake this supervisor, use it to check progress about every five
minutes while you believe at least one worker is actively progressing an
assignment or owes an expected response. This includes workers whose last known
state was active even if they have since stopped or disappeared unnoticed.
Reviewers actively reviewing or awaiting worker revisions count as active work;
an idle reviewer retained after approval does not. Use the available harness
feature according to its actual interface; do not invent commands or assume
every harness supports scheduled wake-ups.

Keep at most one pending self-check for this supervision run. Record its handle
so you can cancel it. Prefer a one-shot reminder rearmed after each check, or a
recurring reminder that can reliably be stopped. The reminder should tell you to
inspect current worker state and mail against the task/revision plan, rather
than assume the workers are still running or automatically message them all.

On waking, reconcile expected activity with the actual panes and inboxes. Look
for missed completion reports, blocked approvals, delivery failures, stalled
progress, or a worker that has vanished. Take the next warranted coordination
action within existing permissions; do not restart or reassign work merely
because five minutes passed. Use the established selector procedure for any
cross-session messages, and report material findings to the user without routine
all-clear announcements.

Rearm only if workers are still expected to progress or respond. Cancel the
reminder when no worker or reviewer is active, work is awaiting only your
acceptance/integration action, work is paused, or the run is only waiting for a
human decision. Cancel it on completion, cancellation, or handoff, and before
leaving supervision. A late reminder must recheck this condition and end without
rearming if there is no active work. Re-enable checks if worker activity
resumes.

If no suitable native feature is available, continue using bounded tuios event
waits during active supervision. Do not create a persistent OS timer or a long
sleep loop to emulate harness reminders, and do not claim unattended monitoring
is active when the harness cannot wake you.

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
impact and report whether additional focused checks are needed. Agree where
integration checks will run: prefer one combined run after merging when safe.
If the reviewer already tested the exact prospective merged tree, reuse that
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

**Merging approved worker work into your assigned supervisor branch is
authorized by this workflow. Do not ask the user to approve that merge again.**
Once the reviewer has approved the exact snapshot and you have accepted it,
proceed with the local merge and required conflict-resolution cycle within the
assignment. This applies to both `tuios ship merge` and the verified path-based
merge below. It does not authorize another target branch, force/reset/clean
operations, remote pushes, or bypassing actual harness permissions. A tooling
approval requirement must still be honored; do not invent an extra
conversational confirmation solely because you are merging worker work.

For a worker on a separate branch, merge its completed work into the supervisor
branch:

```sh
WORKER_SESSION='<worker-session-name>'
WORKER_PANE='<worker-tuios-pane-id>'
tuios ship merge -s "$WORKER_SESSION" -w "$WORKER_PANE" -m '<merge message>'
```

Check the destination before running this command: tuios merges into the
worktree's base branch in the repository's **main checkout**, which must be
clean and have that branch checked out. `--into <supervisor-branch>` changes the
target branch but does not move the operation into the supervisor's worktree. If
the supervisor branch is checked out elsewhere or the destination is dirty,
preserve the checkouts. If the intended supervisor branch is checked out
elsewhere, use that verified checkout directly instead of moving branches or
asking for merge permission again:

```sh
SUPERVISOR_WORKTREE='<absolute-supervisor-worktree-path>'
APPROVED_WORKER_COMMIT='<reviewed-and-accepted-worker-commit-sha>'
git -C "$SUPERVISOR_WORKTREE" merge -m '<merge message>' "$APPROVED_WORKER_COMMIT"
```

Before this path-based merge, verify the target root, per-worktree Git
directory, expected supervisor branch and `HEAD`, clean status/index, and that
the source commit is the exact approved worker tip in the same repository.
Serialize merges and keep both agents at rest. A dirty destination blocks
integration; preserve user changes and report the blocker rather than cleaning
them or switching to a convenient branch. Do not use plain Git after a denied
`ship` operation to bypass permissions.

If this explicit Git merge conflicts, preserve the diagnostic and abort only
your own failed merge in the previously clean target, after confirming no other
work has intervened. Then follow the worker-rebase/reviewer-approval cycle
below. Never reset the supervisor branch or discard someone else's changes.

If merging fails due to conflicts, ask the worker to rebase its branch onto the
current supervisor branch and resolve the conflicts under a new task revision.
Notify the retained reviewer of the target and have it review the resolution and
focused checks directly with the worker. Wait for an updated approval tied to
the resulting snapshot/commit before retrying. Do not resolve feature conflicts
yourself merely to bypass the pair. If you specifically request a merge into the
worker branch instead, require it to stop before creating a merge commit so the
reviewer can approve the result and you can commit it.

After a successful merge, verify the combined result and confirm the worker's
entire assignment is present, with no outstanding changes or revisions. Only
then release the worker and reviewer and remove the worktree. Send each release
to its exact recorded pane, and verify a successful delivery receipt for **every**
recipient before removing the session. For a batch, iterate structured recipient
records using individually quoted argv or Python `subprocess` argument lists.
Do not pack recipient fields into strings, use `set -- $record`, or rely on zsh
word splitting. Record each recipient/result; one successful send does not prove
the rest of a loop ran. Retry only recipients without confirmed delivery, with
fresh verified selectors as needed:


```sh
tuios worktree rm "$WORKER_SESSION"
```

This removes the shared session, worktree, worker, and reviewer. Keep the
reviewer until the worker is no longer needed, and preserve its final report
before removing the session. Do not force removal or discard work. A worker
using the supervisor's current checkout needs no merge; never remove the
supervisor session or checkout as worker cleanup. For a shared current checkout,
release both roles and close only the newly created agent panes once neither is
needed; never remove the supervisor session. Preserve pre-existing
agents/sessions unless their removal is authorized.

Finish by reporting delivered behavior, validation, commits/integration, and any
remaining limitations. Keep responsibility for the combined result until the
user's requested work is complete.
