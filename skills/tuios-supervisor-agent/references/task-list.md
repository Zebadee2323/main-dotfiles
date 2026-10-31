# Supervisor delivery flow and todo pane

Create one dedicated TML todo pane in addition to the live agent tree. Read
`tuios --skill tml` for the installed interface. The supervisor owns this pane
and updates it from its durable task ledger; workers/reviewers supply evidence
through mail. Agent idle/done states and review approval never tick an item.

## Show the full plan

Before delegation, list all known units of work, including work the supervisor
will handle itself and final integration/validation/cleanup milestones. Keep
completed items visible and append newly discovered work as the scope changes.
Use stable task IDs and short outcome-based titles. Do not add a row for each
command, acknowledgment or review round. Group small related changes as one item.

## Show dependencies and parallel work

Build the display from a dependency graph, not just a flat priority list. Assign
stable task IDs, explicit prerequisite gates and planned execution waves. Tasks
in a wave labelled `parallel` may proceed independently; sequential work gets
separate waves or explicit ordering. Include a readable flow summary, such as
`Plan → parser + report (parallel) → serial merges → combined tests → cleanup`.
The summary must describe the actual plan, not the template's placeholder text.

Show an owner/lane for each task and the available worker/build capacity. Planned
parallelism is not the same as running: show `Ready; waiting for worker` or
`Waiting for build slot` when capacity limits it. Never label overlapping writes,
shared mutable outputs or contending exclusive resources as safely parallel.
Serialize Git integration even when implementation was parallel.

Use dependency gates such as `parser:review-approved`, `merge-parser:merged` or
`combined-tests:passed`, not simply another row's checkbox. Delivery checkboxes
can wait for final validation; using them as scheduling gates would deadlock the
validation task. Review can start as soon as its own worker freezes, and a ready
task need not wait for unrelated work in an earlier display wave. Waves explain
the plan; actual dependency and resource readiness control execution.

Reject missing prerequisite IDs and dependency cycles before delegation. On scope
changes, update the graph and display together, retaining completed history. Show
blocked prerequisites by name. If a completed prerequisite is invalidated, revise
the affected downstream statuses and plan before allowing dependent work.

Use the ledger as the source of truth. Each display row has `id`, `wave`, `label`,
`after`, `status` and `mark`. `label` includes task ID, title and owner/lane;
`after` is a readable rendering of structured dependency gates in the ledger.
Sort rows in planned flow order, keeping the order stable across status updates.
For example:

```json
[
  {"id":"parser","wave":"2 · parallel","label":"parser: Parse input · Worker A","after":"plan:accepted","status":"Implementing","mark":"☐"},
  {"id":"report","wave":"2 · parallel","label":"report: Summarize input · Worker B","after":"plan:accepted","status":"Reviewing","mark":"☐"},
  {"id":"merge-parser","wave":"3 · serial","label":"merge-parser: Integrate parser · Supervisor","after":"parser:review-approved","status":"Waiting for review","mark":"☐"},
  {"id":"merge-report","wave":"3 · serial","label":"merge-report: Integrate report · Supervisor","after":"report:review-approved, merge-parser:merged","status":"Waiting for review and merge","mark":"☐"},
  {"id":"combined-tests","wave":"4 · validation","label":"combined-tests: Verify combined tree · Worker A","after":"merge-parser:merged, merge-report:merged","status":"Waiting for both merges","mark":"☐"}
]
```

These rows illustrate an excerpt; include the referenced plan row and cleanup
milestone in the real ledger/view. For narrow panes, shorten labels or use a
stacked list with the same wave, lane and dependency information; keep the flow
visible rather than dropping dependency columns.

Use `☐` for pending/active/blocked items and explicit status text to distinguish
them. Cancelled/superseded items use `—` with an explicit status and remain in
the history. Completed items use `✓`. If adding colors, use semantic TML roles.
The header count includes only checked items; cancelled items remain in the total
and are visibly labelled. Use `run_status` to explain a finished or cancelled run.

For a code delivery item, tick it only when its exact accepted work is present
in the supervisor branch and the required combined acceptance evidence is
sufficient. Record the merge/integrated commit and acceptance evidence in the
ledger first, then patch the pane immediately. After a merge with checks still
pending, show `Merged; validation pending` unchecked. A worker commit or review
approval alone is insufficient. For work in the shared supervisor checkout,
require accepted, committed contents and the same evidence; no artificial merge
is needed. For non-code milestones, use their explicit completion criterion.
Reopen the item visibly if later evidence invalidates completion. Never tick
cancelled work as successful.

## Open once, patch on changes

Copy [the template](../assets/task-list.tml) into the run artifact directory.
Populate its initial state with the current plan before showing it. Generate
state/document values with a JSON/XML serializer; do not interpolate task text
into shell commands or unescaped XML. Use supported TML JSON if convenient.

Resolve the supervisor's actual workspace from its exact `window_id` in
`tuios list-windows -s "$SUPERVISOR_SESSION" --json`. Open from the supervisor's
own pane, with that workspace explicitly; the surface does not take focus:

```sh
tuios tml validate "$TODO_DOCUMENT"
tuios tml show "$TODO_DOCUMENT" --pane --session "$SUPERVISOR_SESSION" --workspace "$SUPERVISOR_WORKSPACE" --name 'Delivery flow' --json
```

Record the returned surface ID and pane ID, plus owner/session/workspace and
artifact paths, in run state. Reuse this surface for the entire run. Do not use
`--wait` or `--events`: this is a supervisor-updated display, not another monitor
process. The template uses display-only checkmarks; user clicks cannot grant
approval or change completion state.

Update the ledger first, then patch `/items`, `/completed`, `/run_status`, `/flow` and `/capacity` in
one batch after meaningful task transitions, scope changes and verified merges.
Build patches from the current ledger so repeated updates are idempotent.
Pass serialized JSON as one argument, for example through Python:

```python
subprocess.run(["tuios", "tml", "patch", surface_id, json.dumps([
    {"op": "replace", "path": "/items", "value": items},
    {"op": "replace", "path": "/completed", "value": completed},
    {"op": "replace", "path": "/run_status", "value": run_status},
    {"op": "replace", "path": "/flow", "value": flow},
    {"op": "replace", "path": "/capacity", "value": capacity},
])], check=True)
```

Keep updates under the installed patch limits; avoid unbounded logs in the pane.
On resume, inspect the recorded surface once and reconcile it with the ledger
before updating. Do not open duplicate panes, poll unchanged state, or ask workers
to patch a surface they do not own. If the user dismisses it, record dismissal
and do not automatically reopen. Report denied/failed display updates without
claiming the pane is current; keep the durable ledger accurate.

At completion, cancellation or handoff, publish the final truthful state and
retain the pane as a snapshot, or close its recorded surface from the owning
supervisor pane if the user requests cleanup. No timer or process needs stopping.
