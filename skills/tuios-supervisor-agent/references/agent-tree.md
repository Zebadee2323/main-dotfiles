# Live TML agent hierarchy graph

Use the bundled `scripts/agent_tree.py` rather than generating a monitor per
assignment. It needs Python 3 and the installed tuios CLI, with no packages.
Read `tuios --skill tml` and `tuios --skill events` for the installed contract.

The display is a connected hierarchy of **labeled box nodes**, not a plain list:
each agent box contains its name, role and live state; worktree boxes group
their workers. Each box uses three rows, with its name in the top border and
its role/state on one interior row. There are no spacer rows between boxes or
blank gaps around the header, leaving more room for the tree in short panes.
Fixed-width boxes and stable ordering keep status changes from
moving the graph. It uses Unicode box-drawing text in a bound TML list of
colored text lines inside a scroll container. Each line has dim connector text
and a box span bound to its role. Role colors are fixed: supervisor `accent`,
worktree `info`, worker `success`, reviewer `warning`. A legend uses the same
roles, and every box includes its role name; do not use arbitrary hex colors or
change a box's role color when its execution state changes. Actual colors follow
the user's theme. In tuios 0.11.0 the `mermaid` component displays source rather than a rendered graph,
and raw Mermaid content does not provide the needed live state binding. Do not
substitute an unrendered `<mermaid>` document. The helper regenerates the bound
graph lines from state; it never asks the supervisor to redraw boxes.

## Ownership and placement

Create **one tree pane in the supervisor's session and actual workspace**,
alongside the supervisor, showing all agents involved across worker sessions.
The helper reads the exact supervisor pane's workspace from `list-windows` and
passes it explicitly to `tml-open`; it does not use whichever workspace the
person happens to be viewing. It does not take focus. Do not create one monitor
or tree pane per worker.

Only the pane that opened a TML surface can patch it. Start the helper as a
background child of the supervisor's tool environment, retaining its actual
`TUIOS_PANE_ID` and `TUIOS_SESSION`. Do not run it in a new shell pane or have
workers patch its surface: those have different owners. Never spoof ownership
environment variables. Required grants are read access to participating agent
states/events and write access for the supervisor's own TML surface. If denied,
report the missing capability; do not change grants or bypass the restriction.

## Registry: the supervisor's only routine input

Keep `agent-tree.json` in the run's absolute artifact directory outside tracked
checkouts. Initialize it with the supervisor alone, then update it **only when
worktrees or participating agents are added, removed, or replaced**. Agent
status, progress messages, review rounds, commits, and test results are not
registry edits. The UI reports execution state, not review approval.

Example shape (substitute recorded full pane IDs, sessions and checked names):

```json
{
  "version": 1,
  "session": "project",
  "supervisor_pane": "<supervisor-full-pane-id>",
  "nodes": [
    {"id": "<supervisor-full-pane-id>", "kind": "supervisor", "label": "Justin", "session": "project", "pane": "<supervisor-full-pane-id>"},
    {"id": "worktree:slug", "parent": "<supervisor-full-pane-id>", "kind": "worktree", "label": "slug", "session": "slug"},
    {"id": "<worker-full-pane-id>", "parent": "worktree:slug", "kind": "worker", "label": "Jaden", "session": "slug", "pane": "<worker-full-pane-id>"},
    {"id": "<reviewer-full-pane-id>", "parent": "<worker-full-pane-id>", "kind": "reviewer", "label": "Tess", "session": "slug", "pane": "<reviewer-full-pane-id>"}
  ]
}
```

The helper supports up to 60 nodes, labels up to 64 characters, and depth 8.
Displayed labels are limited to 32 terminal cells; use short, distinct names.
Use actual pane IDs as agent node IDs, never names or row numbers. Use
`worktree:<recorded-session>` for worktree group IDs. Parents precede children;
keep assignment order stable. Workers in the current checkout can be direct
children of the supervisor. A reviewer is a child of its worker and is added
only after worker readiness and reviewer launch, preserving the deferred-review
rule. Starter shells and the TML pane are not agents and must not be registered.
Use checked agent names when available; a short role/task label is acceptable
at launch before identity is known. A later agent replacement gets its new
pane ID, never the old node's ID.

Write complete JSON to a sibling temporary file and atomically rename it over
the registry (`os.replace`/`Path.replace`), so the helper never reads a partial
write. It validates uniqueness, pane ownership, parent ordering and depth. Do
not use shell interpolation for untrusted names or JSON. Remove agents and their
descendants only after actual release/removal; remove a worktree group after
successful worktree cleanup. Unexpected disappearance stays visible as
`missing` until the supervisor reconciles it; it is never inferred as `done`.

## Start, inspect, and stop

Resolve `TREE_SCRIPT` relative to the actual skill location you read, not a
guessed installation path. Run once from the supervisor's own tool shell:

```sh
TREE_SCRIPT='<absolute-skill-directory>/scripts/agent_tree.py'
TREE_REGISTRY='<absolute-run-artifact-directory>/agent-tree.json'
python3 "$TREE_SCRIPT" start --registry "$TREE_REGISTRY"
```

`start` detaches the helper, returns after its runtime file is ready, and prints
the PID, surface ID, UI pane ID, owner, session, workspace and monitor state.
Record these with the supervision run. The helper holds a process-lifetime
lock and refuses a duplicate start. Do not retry a pending start blindly.
Its sibling files are `agent-tree.runtime.json`, `agent-tree.log`,
`agent-tree.lock` and a transient `agent-tree.stop` request.

Do not rebuild or manually patch the tree for ordinary status changes. The
helper subscribes once to `agent-state`, window lifecycle and TML-close events
**before** taking the initial state snapshot. Native harness hooks and workers'
own `tuios set-agent-state` reports flow through that subscription. It filters
exact session/pane bindings, coalesces bursts, and patches only changed labels
and the affected box text lines. Topology edits replace `/nodes` and `/lines`;
the TML component document stays fixed. Large patch batches are split below the
wire limit.
The only idle polling is the local registry/stop file check, with no periodic
daemon state scans. This is a UI publisher, not a supervisor reminder or an
agent: it never reads mail, reviews work, issues instructions, changes agent
states, or makes acceptance decisions.

On a stream gap or reconnect, it reads a fresh canonical snapshot. A disconnect
shows `reconnecting; status stale`; at most three reconnects per helper run are allowed before
stopping with a visible stale banner and a log error. A closed/dismissed surface
stops the helper instead of reopening the pane. Do not claim monitoring is live
if the runtime/log says otherwise. Inspect only when there is a failure or a
structural change, rather than continuously checking the monitor yourself.

At completion/cancellation/handoff, finish the final topology update, then:

```sh
python3 "$TREE_SCRIPT" stop --registry "$TREE_REGISTRY"
```

The helper exits, marks the retained tree `stopped`, and ends its subscription.
Keep the final tree as a snapshot for the person, or close the recorded surface
from its owning supervisor pane with `tuios tml close '<recorded-surface-id>'`.
It must not silently keep monitoring after the supervision run ends. For a
deliberate restart, confirm the old helper has stopped, inspect/close its old
surface, and archive/remove only this run's runtime file before calling `start`
again. Never remove a live lock or kill an unrelated process to force restart.
