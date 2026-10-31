# Durable task state and review evidence

The supervisor owns one `tasks.json` outside tracked checkouts. Write a complete
replacement to a sibling temporary file and atomically rename it. Other roles
send updates; they do not concurrently edit the ledger. This is delivery state,
separate from the live graph's execution state. Keep current facts here and large
logs/diffs in referenced artifacts, not in messages or conversation history.

Each task record contains:

- `task_id`, `revision`, `review_round`, `state`, `next_owner`, `next_action`.
- Absolute checkout, per-worktree Git directory, branch, worker and reviewer
  session/pane/checked name, selected harness/models.
- Owned paths, dependencies, acceptance criteria, constraints, validation plan.
- Immutable `implementation_base_sha`; separately `integration_target_sha` and
  `integration_generation` (increment when that target changes).
- Frozen snapshot path/id, completion-message id, reviewer approval-message id,
  assessed integration target, validation evidence paths, unresolved findings.
- Review checkpoint budget, elapsed rounds, escalation/decision and release state.

Use these states: `preflight`, `implementing`, `ready_for_review`, `reviewing`,
`revising`, `approved`, `integrating`, `integrated`, `released`, `blocked`,
`superseded`. Record the responsible next owner on every transition. `blocked`
includes its prior state and the decision needed. Review approval requires the
recorded reviewer's direct message and matching revision/snapshot. `integrated`
requires combined acceptance evidence, not merely successful Git merge.

Before compaction, replacement or handoff, update the ledger and each role's
short continuation artifact: current assignment, unresolved findings, exact
snapshot/evidence paths, pending request and next action. On resume reconcile
ledger with actual Git state and unread mail; never replay a mutation merely
because the previous conversation ended before reporting it. Verify existing
commits/merges before retrying. Preserve artifacts until final release.

## Snapshot helper

Resolve `scripts/review_snapshot.py` from the supervisor skill and give its
absolute path to the reviewer with the artifact directory. No sibling skill
installation lookup is required. Run it; do not load its source routinely.

```sh
python3 "$SNAPSHOT_TOOL" capture --root "$CHECKOUT" --base "$IMPLEMENTATION_BASE_SHA" --manifest "$MANIFEST"
python3 "$SNAPSHOT_TOOL" verify --root "$CHECKOUT" --manifest "$MANIFEST"
python3 "$SNAPSHOT_TOOL" verify-commit --root "$CHECKOUT" --manifest "$MANIFEST" --commit "$COMMIT_SHA"
```

Capture after all writers stop; verify immediately before committing; verify the
committed tree afterward. Exit 0 means match, 1 mismatch, 2 unsupported/error.
The helper never stages or changes Git state. It records raw content hashes,
executable/symlink modes, index/status fingerprints, base/head/branch/worktree
identity and pending merge state. It detects changes between two capture reads;
this is not a filesystem lock. Keep writers stopped through commit verification.
Check status after committing too; hook-created leftovers need review.

Commit comparison covers the entire tree, including new files. Clean/smudge
filters, LFS pointers, line-ending normalization and executable-bit conventions
can make raw checkout bytes differ from committed blobs. A mismatch is never
approval: the reviewer must explicitly establish the repository's canonical
representation and record that evidence, or report unsupported verification.
Submodules and active rebases are rejected rather than silently omitted. Ignored
files are excluded unless tracked; they are not part of `ship commit` approval.
The manifest is evidence, not a permission grant or substitute for code review.

## Keep reviews bounded

Default checkpoint: three batches of revision requests per assignment, or a
smaller/larger explicit budget chosen for the task. At the checkpoint the reviewer
summarizes remaining findings and asks the supervisor whether to continue, split,
clarify, or replace the assignment. This never means automatic approval or
lowering criteria. Escalate sooner after two requests on the same issue with no
meaningful progress. Track current findings with stable IDs, severity, location,
required evidence and resolution; archive resolved discussion. A continuation
handoff preserves unresolved findings and links rather than full transcripts.
