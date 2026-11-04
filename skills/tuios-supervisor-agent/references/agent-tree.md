# Live agent-comms tree

Use the built-in silent watcher, with one compact line per node, plus any optional info line. Name each run's
tree uniquely (for example `project-review-20261101`). Only the creating identity
can replace/delete it; other agents can view it. Tree and roles persist across
listener restarts, and clear with an explicit identity-bag reset.

Set your own role after obtaining your checked identity:

```sh
agent-comms role supervisor
```

Require workers to run `agent-comms role worker` and reviewers to run
`agent-comms role reviewer` during preflight. Roles are free-form display text;
they confer no permissions. They are independent of `send --role requester|responder`.

Write a JSON array in the run artifact directory using current verified names:

```json
[
  {"id": "supervisor", "agent": "Dennis"},
  {"id": "engine", "parent": "supervisor", "label": "engine"},
  {"id": "worker-engine", "parent": "engine", "agent": "Debbie"},
  {"id": "reviewer-engine", "parent": "worker-engine", "agent": "Dale"}
]
```

Substitute actual identities; omit reviewer nodes until the reviewers exist.
Grouping nodes have labels; agent nodes use the bound identity and its self-set
role. Node IDs are stable within the tree; parents refer to IDs in the same array.
Publish the full array atomically when membership, hierarchy, labels, or `info`
descriptions change:

```sh
agent-comms tree set '<run-name>' --file '<absolute-run-artifact-directory>/tree.json'
agent-comms tree show '<run-name>'
```

Read `tuios --skill` and the installed pane-launch help. Open one shell pane in
**your own session and actual workspace**, with this foreground command, using
supported launch and placement options without stealing focus:

```sh
agent-comms watch --view tree --tree '<run-name>'
```

Record the returned pane ID. The watcher is silent and shares the listener's
presentation timeline with captions and conversation. It reconnects and restores
the latest full tree automatically. Queue counts indicate pending messages;
`◌` means preparing and `▶` means displaying. Speaker and recipient are highlighted,
with a connector arrow pointing to the recipient. Participants outside this tree,
including the human, appear temporarily during presentation. Narrow panes clip
labels; short panes keep the speaker visible and show an omitted-line count.
This describes communication activity, not implementation status or approval.
Continue using the durable ledger and delivery-flow pane for task progress.

The supervisor owns keeping the published tree accurate throughout the run. On
launch, reassignment, replacement, release, or cleanup, reconcile the affected
nodes and descriptions with the actual team. Remove departed agents or clearly
label retained historical entries so they do not appear active. If new evidence
makes a label, title, or `info` description stale, correct and publish it before
your next progress update or wait. Update the full JSON array with `tree set --file`
and the title with `tree set --title` as needed; editing the file alone is insufficient.
Use `tree show` to verify the published result.

The listener updates communication indicators automatically; no per-message tree
updates or background monitor process are needed for those indicators. Do not ask
workers to edit a shared registry. Keep the final tree
for inspection, or delete it with `agent-comms tree delete '<run-name>'` and close
only the recorded watcher pane through normal tuios commands when appropriate.
Do not stop the shared listener. Report denied pane operations without bypassing grants.

Nodes may also include optional `info` text (up to 160 printable characters).
It appears as a dim, indented line beneath that node; omit it or use an empty
string to keep the node on one line. For example:

```json
[{"id":"engine","label":"engine","info":"Implementing the new calculation engine"}]
```

Use it for a worktree's feature or the supervisor's current understanding of an
agent's work. Replace the tree when those descriptions change; roles remain
self-managed by each agent. Info stays on one line and clips to the pane width.

Set a main screen title with
`agent-comms tree set RUN_NAME --file tree.json --title 'Calculator implementation'`.
For an existing tree, `agent-comms tree set RUN_NAME --title 'New title'` changes
only its title. Titles are printable text up to 160 characters, persist with the
tree and update live in every watcher. Omitting `--title` preserves the current
title; `--title ''` restores the tree name as the heading. Titles clip to the
pane width and share the existing header row, using no extra vertical space.
