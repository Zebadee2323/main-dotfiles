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

After a successful merge, update the todo item to `Merged; validation pending`
while required checks remain. Once integration and acceptance are verified,
record the evidence and tick the item immediately in the supervisor's Delivery
flow todo pane; keep the completed row visible through cleanup.

After a successful merge, delegate required combined integration tests to a
named worker, identifying the exact merged commit/tree, checkout, checks and
reviewer. The worker executes and reports evidence; the reviewer assesses it;
you accept the combined result. Do not execute combined integration tests
yourself. Reuse adequate evidence for an identical tree and preserve checks
reserved for the user. Confirm the worker's
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

## Combined failures without Git conflicts

A clean merge is not combined acceptance. If integrated behavior fails, the
supervisor owns triage and assigns a specific repair owner, affected paths,
acceptance evidence and reviewer(s). Keep needed pairs alive. Prefer one worker
for shared-interface repairs; serialize overlapping writes. Update the affected
task revision or create a linked repair task, and return fixes through review.
The supervisor coordinates architecture and integration checks, not unreviewed
feature fixes. Release pairs only when their work and combined requirements pass
or the user explicitly accepts a documented limitation.

When only the integration target advances, retain implementation approval and
request a delta assessment tied to the new target SHA/generation. Review actual
interaction risk and run only warranted checks. Conflict resolutions or code
changes require a new task revision and snapshot approval.
