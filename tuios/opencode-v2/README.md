# OpenCode V2 → TUIOS conversations

Local CLI plugin for OpenCode **2.1.4** and TUIOS **0.11.0** APIs. No npm install
is needed: OpenCode supplies `@opencode/plugin/tui` and `solid-js` at runtime.

## Why the existing integration misses conversations

Codex's hooks report its session ID **and transcript path**. TUIOS can read the
conversation directly, independently of the process serving the harness.

The installed OpenCode integration already has a V2 terminal adapter, but only
reports the session ID. TUIOS's bundled `opencode-service` reader looks for
`{state}/opencode/service.json`, then queries that registered background service
or falls back to spawning an export process. This is a poor fit for private
Sidekick servers, remote servers, and differently located service registrations.
In particular, the local OpenCode configuration contains `service.json` in
`~/.config/opencode`, whereas the bundled TUIOS manifest searches the state dir.
These are concrete integration mismatches, not evidence of a live UI reproduction.

This plugin runs **in the local terminal**, uses its existing authenticated V2
client/cache, and mirrors the selected conversation into a local private file.
It reports the exact path to TUIOS, just as Codex does. No server credential files,
database reads, export processes, network listeners, or guessed service URLs.

## Install

The companion harness manifest is required: a JS plugin alone cannot change
TUIOS's built-in transcript reader.

```sh
python3 ~/.config/dotfiles/tuios/opencode-v2/install.py
python3 ~/.config/dotfiles/tuios/opencode-v2/install.py --apply
```

The first command previews. The second:

- Adds this directory to `~/.config/opencode/cli.json` and disables the old
  `tuios-agent-state` CLI plugin with enablement directives. Keeps other settings,
  makes a one-time `.before-tuios-v2` backup, and preserves a config symlink.
- Installs `opencode.toml` in `~/.config/tuios/harnesses/`. Respects
  `XDG_CONFIG_HOME` and `TUIOS_HARNESS_DIR`; upgrades this plugin's exact original
  snapshot-reader manifest, but refuses a differing custom manifest.
- Does **not** edit TUIOS-managed plugins, your existing `tuios/config.toml`,
  approvals, or permission grants. Does not restart anything.

OpenCode reloads the CLI config automatically; `/reload` forces reloading.
TUIOS loads harness manifests at **daemon startup**. When safe, run
`tuios restart-server` yourself, then launch/reopen OpenCode in a TUIOS pane.
Do not run `tuios integration uninstall opencode`: the old files can stay installed
but disabled. Its status command will describe the stock integration, not this one.

Manual configuration, instead of the installer:

```json
{
  "plugins": [
    "*",
    "-tuios-agent-state",
    "-tuios-agent-state-v1",
    "/Users/onelson/.config/dotfiles/tuios/opencode-v2"
  ]
}
```

Merge that into `cli.json`; copy the companion manifest into the harnesses dir.
Do not add the plugin to server config: CLI configuration also works against a
private or remote server. Both the CLI and TUIOS daemon must agree on
`XDG_STATE_HOME` (default `~/.local/state`). Requires the actual `TUIOS_PANE_ID`
environment variable; outside TUIOS it is a no-op.

## Behavior and limits

- Mirrors the **displayed session only**. Background tabs/subagents cannot mark
  it done. A deliberately opened child conversation can be displayed normally.
- On selecting a session or changing its title, runs `agent-comms context` in the
  terminal client's pane environment. OpenCode keeps ownership of its native
  terminal title; the refresh is deferred until its reactive title effects run.
  Agent-comms then normalizes the identity prefix over the live title. Rapid
  changes coalesce, lookups are serialized and bounded to 10 s, and failures warn
  once without blocking mirroring. No polling, automatic retries, service startup,
  identity caching, or changes to tuios's Thread cache are involved. Requires the
  updated agent-comms listener; `/reload` loads this plugin change.
- Preserves the real OpenCode session ID for resume. Serializes state reports,
  guards against stale hydration after tab switches, handles multiple pending
  permissions/forms, failures, interrupts, and cumulative model/cost metadata.
- Sends only recent prompts/replies, attachment placeholders, tool names/status
  and brief errors. **No reasoning, raw tool arguments, tool output, patches,
  image data, shell commands, system prompts or native provider records.** Full
  details remain in OpenCode, not in a second TUIOS copy.
- Uses TUIOS's existing `gemini-chat` **JSONL encoding**, not the Gemini harness.
  Stable keys replace individual content parts as they stream. OpenCode is still
  identified/resumed as OpenCode; no TUIOS rebuild is needed. Re-run the installer
  and safely restart TUIOS if upgrading from the initial `amp-thread` prototype.
- Ordinary stream events coalesce over 750 ms. Only changed parts are appended;
  unchanged content, permission-only changes and unrelated metadata produce no
  transcript writes. Turn/blocker boundaries flush pending content immediately.
- A persistent append handle avoids repeated open/rename operations. Initial
  files and occasional compactions are atomic, mode `0600`, in a mode `0700`
  directory. Handles close on tab switches/unload. Cold history loading yields
  to the terminal event loop every 32 projected messages.
- Files live under `${XDG_STATE_HOME:-~/.local/state}/tuios/opencode-v2/`, named
  by a hash of pane ID. **One file is reused per pane**, rather than accumulating
  files for every session/tab switch. Keeps up to **40 recent prompts/replies**
  plus **8 recent tool-only steps**, so a busy tool loop cannot displace the user's
  prompt. At most 8 tool summaries are kept per assistant message. Text is capped
  at **4 KiB per message** across its text parts, with a visible truncation marker;
  errors are one line, at most 256 bytes. Only the first 256 content parts are
  considered. Encoded live history is capped at **256 KiB**.
  Obsolete revisions are compacted before an append would exceed approximately
  twice the live size plus 64 KiB (**at most about 576 KiB per pane**, plus a small
  header). This is still a **local plaintext copy** of the selected content, including any
  secrets present in that data. Files persist after exit; remove unwanted
  pane journals yourself. Old `.json` prototype snapshots are not auto-deleted.
  OpenCode's database remains authoritative. This reduces the TUIOS mirror, not
  OpenCode's own conversation storage/cache. No rich-data mirroring mode is enabled.
- Filesystem and state reporting are independent. Slow I/O cannot hold up an
  approval-state report. Each writer has one in-flight operation and coalesced
  dirty IDs, not a queue of copied snapshots. The reporter has one in-flight
  subprocess and at most three pending telemetry snapshots. Under extreme
  overload, intermediate telemetry is coalesced, retaining the latest turn-end
  and current state rather than an ever-growing backlog.
- Approval/question state is reported, but answers stay in OpenCode's own UI.
  This plugin **never approves, rejects, submits, or cancels** a request. It does
  not implement TUIOS Inbox approval forwarding or Codex-style activity hooks.
- A filesystem failure warns once and leaves state reporting functional. Missing
  or unresponsive `tuios` cannot crash OpenCode; commands time out after 2 s.

### Startup warnings and diagnostics

Permission/form cache refreshes do not gate transcript startup. A failed history
refresh falls back to cached messages and live events rather than permanently
disabling the mirror. Canceled requests and failures from an old tab are ignored.
New-session `SessionNotFoundError` refresh failures retry after 100, 250, 500 and
1000 ms before warning, allowing session creation to finish. Retries stop on a
tab switch or plugin unload; other errors warn immediately.
Warnings identify the failing operation and an error code/status instead of
calling every failure a filesystem problem.

On a genuine failure, a small private `<pane-hash>.jsonl.diagnostic.json` file
beside the journal records the operation, time, error type/code/status, and an
optional plugin filename/line. It is overwritten, not appended. It excludes raw
error messages, URLs, full stacks, prompts, tokens and tool contents. This is a
diagnostic file only; it is not passed to TUIOS's conversation reader.

## Verification

```sh
cd ~/.config/dotfiles/tuios/opencode-v2
npm test
npm run benchmark
# Optional actual temporary-file I/O, instead of counting logical writes:
npm run benchmark -- --disk
```

Tests cover projection/private writes, session isolation, ordering, blockers,
late hydration, switches, failures, partial-write recovery, compaction, bounded
inputs/queues, slow I/O and 10,000-event bursts. See [PERFORMANCE.md](PERFORMANCE.md)
for benchmark methodology, measurements and remaining costs. These do not constitute
an end-to-end TUIOS conversation-screen test. After installation, verify with a
short conversation, a permission/form, a tab switch and your Naia private-server
launcher. `tuios get-agent-state -w "$TUIOS_PANE_ID"` should show the selected
OpenCode session; the TUIOS conversation screen should show its prompt/reply.

## Undo

Remove this plugin entry and the two disable directives from `cli.json`, or
restore the backup **only if you have made no subsequent config edits**. Remove
only the companion `harnesses/opencode.toml` installed above, and restart the
TUIOS daemon when safe. Stock integration files were never modified.

## Contracts consulted

- [OpenCode V2 CLI plugin API](https://opencode.ai/v2/docs/build/plugins/cli)
- [OpenCode V2 CLI plugin loading](https://opencode.ai/v2/docs/cli/plugins)
- [TUIOS Codex event adapter](https://github.com/Gaurav-Gosain/tuios/blob/main/internal/integration/hook_codex.go)
- [TUIOS OpenCode manifest](https://github.com/Gaurav-Gosain/tuios/blob/main/internal/harness/manifests/opencode.toml)
- [TUIOS service transcript reader](https://github.com/Gaurav-Gosain/tuios/blob/main/internal/transcriptview/opencode.go)
- [TUIOS keyed JSONL reader](https://github.com/Gaurav-Gosain/tuios/blob/main/internal/transcriptview/gemini.go)
- [TUIOS exact-path allowlist](https://github.com/Gaurav-Gosain/tuios/blob/main/internal/session/verb_handlers.go)
