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
