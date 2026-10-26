# agent-comms

Shared agent identities and a per-user FIFO message listener, with live colored
tuios dock output. Python **3.11+**, standard library only. See [SPEC.md](SPEC.md)
for the design.

## Run

macOS/Linux, from this directory:

```sh
chmod +x agent-comms
./agent-comms serve
```

In another terminal:

```sh
ID=$(./agent-comms id)
PREFIX=$(./agent-comms prefix --id "$ID") || exit 1
./agent-comms send --id "$ID" --msg "${PREFIX:+$PREFIX }Hello from the agent dock!" --tts "${PREFIX:+$PREFIX }Hello from the agent dock!"
./agent-comms watch
./agent-comms history
```

For a command available everywhere, put this directory on PATH or symlink the
launcher (keep the source directory in place):

```sh
mkdir -p ~/.local/bin
ln -s "$PWD/agent-comms" ~/.local/bin/agent-comms
```

On Windows, use `agent-comms.cmd` (requires the Python `py` launcher), or run
`python agent_comms.py ...` directly. Add this directory to PATH to use
`agent-comms` from other directories. Windows support is implemented but has not
been execution-tested yet. Modern Windows Terminal is recommended for ANSI
colors.

```powershell
$id = .\agent-comms.cmd id
$prefix = .\agent-comms.cmd prefix --id $id
if ($LASTEXITCODE -ne 0) { throw "Could not check prefix" }
$message = if ($prefix) { "$prefix Hello from Windows!" } else { "Hello from Windows!" }
.\agent-comms.cmd send --id $id --msg $message --tts $message
```

## Commands

| Command | Behavior |
| --- | --- |
| `id` | Print only the next ID; refill and shuffle automatically when empty. |
| `id --refill` | Replace remaining entries with a new shuffled bag; no draw/output. |
| `serve` | Own the singleton listener, timed FIFO queue, and terminal display. |
| `serve --quiet` | Same listener without terminal display. |
| `prefix --id ID` | Print the exact required introduction, or an empty line if none is needed. |
| `prefix --id ID --json` | Print the prefix, requirement flag, and checked tuios context. |
| `send --id ID --msg TEXT --tts SPEECH` | Save and queue clean display text plus separate speech text; acknowledge acceptance, not completion. |
| `history` | Print the rolling history of at most 10 accepted messages, even offline. |
| `history --limit 5 --id nova --json` | Filter/limit retained history and optionally include complete metadata and speech text. |
| `history --session work` | Filter by the sender's tuios session name. |
| `watch` | Show flashing, timed caption chunks and optionally generate/play speech. |
| `watch --no-color --width 80` | Plain-text output, at most 80 display cells. |
| `watch --mute --no-flash` | Disable speech generation/playback and the attention flash. |
| `session --harness opencode --session-id ID` | Resolve/persist a session identity and print JSON metadata plus messaging instructions. |

`session --format text` prints just the instruction block. Plugins use this
helper instead of repeatedly drawing new IDs.

`NO_COLOR` also disables watcher colors. Watch width defaults to 96 cells.
Errors go to stderr and return a nonzero exit status. Offline sends do not launch
a server. Offline watchers show `[! ] comms: offline` and reconnect with backoff
from 250 ms to 5 seconds. Ctrl+C stops the foreground command. The server also
handles SIGTERM for clean shutdown where the OS delivers it.

## Configuration and locations

Edit [config.toml](config.toml) in this directory. It is the default on all
platforms, resolved relative to the program rather than your working directory
(including when launched through a symlink). Restart the server after changing it.
An explicitly selected missing config is an error; it is not silently generated.
Watchers receive voice IDs from the listener's message metadata and read optional
speech model/speed settings from this file; restart watchers after changing those
settings. Missing local config falls back to the Naia speech defaults.

```toml
display_seconds = 5

[speech]
model = "eleven_turbo_v2_5"
speed = 1.0

[identities.naia]
name = "Naia"
eleven_labs_voice_id = ""
personality = "Friendly, clear, and practical."
```

The starter roster is Naia, Nova, Atlas, and Echo. Fill each
`eleven_labs_voice_id` with a voice available to your ElevenLabs account to enable
that identity's speech. Blank voice IDs deliberately remain text-only. Plugins
must use personality metadata for agent-comms messages only, **never for normal
assistant responses**. IDs are not exclusive reservations.

Default locations:

| Platform | Config | Bag state | Discovery and server lock |
| --- | --- | --- | --- |
| macOS | `config.toml` beside the program | `~/Library/Application Support/agent-comms/state/` | `~/Library/Application Support/agent-comms/state/agent-comms-run/` |
| Linux | `config.toml` beside the program | `~/.local/state/agent-comms/` | `$XDG_RUNTIME_DIR/agent-comms-run/`, otherwise `~/.local/state/agent-comms/agent-comms-run/` |
| Windows | `config.toml` beside the program | `%LOCALAPPDATA%\agent-comms\state\` | `%LOCALAPPDATA%\agent-comms\run\` |

macOS/Linux honor `XDG_STATE_HOME` and `XDG_RUNTIME_DIR` when set. Windows falls
back to the user's `AppData/Local` if `LOCALAPPDATA` is absent. Config location is
independent of XDG settings; use the explicit config override below if needed.

Overrides, accepted before or after the subcommand:

| Flag | Environment variable |
| --- | --- |
| `--config PATH` | `AGENT_COMMS_CONFIG` |
| `--state-dir DIR` | `AGENT_COMMS_STATE_DIR` |
| `--runtime-dir DIR` | `AGENT_COMMS_RUNTIME_DIR` |

Use **dedicated user-private directories** for state/runtime overrides. On POSIX,
the program enforces mode 0700 on state/runtime directories; data files are
created with mode 0600. It does not modify config directory permissions or create
config lock files. On Windows, files inherit the containing user's profile ACLs;
do not redirect them into a shared directory. The token protects against other
local users, not processes already running as you.

The locked `bag.json` stores the roster and remaining IDs. Changing the set of
configured IDs starts a new cycle; changing metadata alone does not. Corrupt or
invalid bag contents are replaced with a fresh shuffled cycle. Invalid JSON
also produces a warning on stderr. The separate lock file is never replaced or
deleted while updating bag contents atomically.

## Transport and lifecycle

One server per selected per-user runtime directory binds only `127.0.0.1` on an
ephemeral port. `server.json` atomically advertises protocol version, port, and a
random 256-bit token. **Treat this file as a secret.** Clients reread it when
connecting. There is no manual port assignment, remote listener, or native
Windows/WSL discovery bridge.

The transport is newline-delimited JSON. Initial requests contain
`protocol_version`, `token`, and `command`. **Protocol version 2** adds distinct
`tts`, unique message IDs, persisted acceptance, sender context, and speech leases.
Restart old listeners/watchers when upgrading. `send` carries `id`, `msg`, optional
`tts` (defaults to `msg` for compatibility), and optional sender context.
Discovery also advertises `prefix_policy: "explicit"`. The CLI refuses to send
to a pre-update auto-prefixing listener; restart that listener first. Watcher wire
format is unchanged, so current watchers reconnect without a protocol upgrade.
Successful replies include `ok: true` and the protocol version. Watch replies
have `event: "state"` and `message`, either null for idle or a message with
`uid`, `id`, `name`, `msg`, `tts`, `voice_id`, and presentation phase. Every
subscription receives an immediate snapshot. There are no polling heartbeats
while connected and idle.

Limits: 8 KiB UTF-8 each for display and speech text (including introductions),
64 KiB per wire frame, 256 pending messages in
addition to the active one, and 128 concurrent connections. Initial requests,
connects, and writes have five-second deadlines. A slow watcher cannot block the
queue; overflowing its 16-update buffer disconnects it so it can resubscribe.
Incoming terminal escapes and invisible/control characters are stripped. Only
the renderer generates ANSI styling.

Clean shutdown removes discovery data while still holding the singleton lock.
A crash releases the lock; the next server replaces stale discovery data and
watchers reconnect. Accepted-message history is durable; the active presentation
and pending delivery queue are **not durable**. Do not
automatically retry a failed send: a lost acknowledgement can leave acceptance
uncertain, and sends have no delivery deduplication.

## Message history

`agent-comms history` works from any working directory and does not require a
running listener or identity config. Default output is the newest 10 messages,
printed oldest-to-newest with UTC acceptance timestamps and identity names.
`--limit` accepts 1–10; `--id`, `--session`, and `--json` are optional. Filters apply
within those ten retained messages, not a separate per-identity/session history.

Private `state/history.jsonl` contains **at most ten entries**. Each acceptance
atomically replaces it with the newest ten messages, flushing before acknowledging
the send. A full queue or failed history write rejects the send. An OS-backed
lock coordinates writers; readers skip incomplete/corrupt rows. History
records acceptance, **not proof of display or playback**; a server crash can leave
accepted but undelivered messages in history. Full records include TTS text and
sender metadata. No API key, authentication token, or speech lease is saved.
Older entries are dropped automatically; treat retained history as private data.
Messages sent before history support was installed cannot be recovered.

## ElevenLabs speech and timed captions

Watchers use **only** their `ELEVENLABS_API_KEY` environment variable for the API
key. No key is read from config, sent to the listener, written to history, or
included in diagnostics. Providing the key enables external ElevenLabs requests
for identities with a configured voice ID and an available local audio player.
The TTS text is sent to ElevenLabs; keep sensitive content out of announcements.

`eleven_turbo_v2_5` matches `nvim/lua/features/voice/runtime.lua`'s Naia default.
Use `--tts` for speech-friendly spelling, punctuation, and supported markup such
as `<break time="0.3s" />`. Turbo does **not** support v3-only `[warm]`/`[excited]`
audio tags. Keep `--msg` plain, readable, and equivalent in meaning:

```sh
prefix=$(agent-comms prefix --id "$ID") || exit 1
agent-comms send --id "$ID" --msg "${prefix:+$prefix }Build finished. Tests passed." \
  --tts "${prefix:+$prefix }Build finished. <break time=\"0.3s\" /> Tests passed."
```

The API returns 24 kHz mono PCM; the watcher wraps it in a temporary private WAV,
measures its exact sample duration, and deletes it after playback/cancellation.
Playback uses `afplay` on macOS, `ffplay` or `aplay` on Linux, or standard-library
`winsound` on Windows. No Python audio dependencies are needed. Speech settings
accept a model name and speed 0.7–1.2. The request timeout is 25 seconds and audio
is bounded to ten minutes. API/player failures fall back to text captions without
logging sensitive request details or automatically retrying paid synthesis.

The listener grants a single watcher a per-message speech lease. Other watchers
still display the same message, but do not generate duplicate audio or charges.
The listener allows 0.4 seconds for a claim and at most 35 seconds for preparation.
`speech-ready` reports the measured duration or a failure; only that message's
current lease is accepted. Reconnecting/joining watchers do not replay speech that
has already started. If the speaker exits mid-message, text continues; that
message's speech is not reassigned/replayed. `--mute` prevents a watcher from
claiming speech at all. Detaching a speaker stops its local playback.

Caption wrapping and weighting port Naia's `features/voice/captions.lua`: roughly
180 words/minute, estimated as **15 non-whitespace characters per second**. Wrap
at word boundaries, split oversized paths by Unicode characters without detaching
combining marks, and assign each page a cumulative character weight. Dock pages
are one row rather than Naia's three rows. All pages replace one another rather
than permanently truncating the message. Long identity names are shortened to
leave caption space.

Without speech, the display lasts at least `display_seconds` (default 5) or the
estimated speaking duration, whichever is longer. With speech, caption progress
is distributed across measured audio duration. All watchers share the listener's
monotonic local-clock start/duration, including a 200 ms preparation-to-playback
delay. This is estimated caption alignment, not ElevenLabs word timestamps.

New messages briefly flash using inverse/bold ANSI SGR (0.6 seconds total).
The flash occurs once per new message ID, not on every caption/state update.
`--no-flash`, `--no-color`, or `NO_COLOR` disables the visual flash. Idle watchers
do not tick/redraw; only active messages animate caption/attention frames.

## tuios session introductions

`prefix --id ID` looks for `TUIOS_ENV=1`, `TUIOS_PANE_ID`/`TUIOS_WINDOW_ID`, and
`TUIOS_SESSION`, then uses read-only `tuios session-info` and `tuios list-windows`
commands to verify the sender pane, current workspace, attachment, focus, and
other agent panes on the same workspace. A two-second timeout bounds each query.

The command returns the exact prefix to put in both display and speech text when the sender's workspace is
not visible (the session is detached or another workspace is selected), **or**
the pane is unfocused and that workspace has multiple agent panes:

```text
Nova here, of the work session. ...
```

An unfocused sole agent does not need the extra introduction. Shell/editor-only
panes are not counted as agents. The sender itself counts even if detection is
still catching up. Outside tuios, the command returns an empty line. If tuios
environment is present but incomplete, the pane is unrecognized, or a query
fails/is forbidden, the command returns an error instead of guessing an empty
prefix. No pane focus or user permissions are changed; forbidden calls are not
retried through another route.

**Nothing is automatically inserted.** Before every message, agents must query
`prefix`, then put its exact result once at the start of both `--msg` and `--tts`
(before any speech markup), followed by a space and their message. An empty result
means no introduction. The prefix wording is fixed, not personality-styled; the
message that follows still uses the identity's personality.
The prefix already identifies the agent and session: do not repeat a
self-introduction in the body, even on the first message. Always introduce yourself
on the first message of a session: use the prefix if nonempty; otherwise introduce
yourself in your own words in both display and speech text. On later messages, an
empty prefix does not call for another introduction.

`send` queries current context again and checks both strings. Missing, stale,
wrong, or duplicated introductions return a nonzero error naming the offending
field and current expected prefix. No message is queued, recorded, displayed, or
synthesized. The listener also validates the strings against the submitted context
snapshot before acceptance; it never prepends or rewrites them. This is a workflow
check, not proof against an authenticated client falsifying its own context.

Focus can change between lookup and send. On mismatch, query again and recompose
once rather than blindly retrying the previous strings. If context cannot be
checked, do not submit the message. Prefix lookup itself needs no live listener
or new identity draw and works from any directory.

## tuios bottom dock

The listener and dock watcher are intentionally separate. Each tuios client runs
its own watcher; only the listener owns the queue, presentation timing, and
exclusive speech lease.

This dotfiles configuration adds the component alongside the existing center
components:

```toml
[dock]
center = ["custom/agent-comms", "windows"]

[dock.custom.agent-comms]
command = 'exec "$HOME/.local/bin/agent-comms" watch --width 80 --flash'
refresh = "push"
max-width = 80
```

The existing dock remains at the bottom. Each stdout line replaces the component
text; SGR colors survive, but cursor movement and clearing do not. This is not an
embedded terminal or a full-width/multi-row rail. Other components share the bar,
and tuios may truncate it further. This tuios version caps custom components at
80 cells, so the integration requests 80 even though standalone `watch` defaults
to 96. The quiet idle display is `[o_o] comms: idle`.

The initial setup runs `serve` in its own detached tuios session named
`agent-comms`. Detaching other tuios clients does not stop it. Inspect it with
`tuios attach agent-comms`. A tuios **daemon restart** ends its running process;
restart `serve` manually in that session after restoration. No OS login service,
startup hook, or automatic supervisor is installed in v1. Alternatively run
`agent-comms serve` in an independent terminal; the singleton lock prevents
accidental duplicate listeners.

On Windows, use a local command such as `py -3 C:/path/to/agent_comms.py watch` in
the dock table rather than copying the POSIX `exec` command above.

## Tests

```sh
python3 -m unittest discover -s agent-comms -v
```

From this directory, `python3 -m unittest -v` also works. Tests use isolated config,
state, discovery, and ephemeral ports; they do not touch the live dock/server.

## OpenCode V2 and Codex integration

Install the stable `agent-comms` launcher on PATH first (see above), then run:

```sh
python3 agent-comms/install_plugins.py
```

This command is run from the parent `tuios` directory. From this directory, use
`python3 install_plugins.py`. On Windows, use `py -3` instead of `python3`.
Pass `--only opencode` or `--only codex` to install just one adapter. The installer
does not change tool permissions, existing hooks, or hook trust.

### OpenCode V2

The installer generates a loader in
`~/.config/opencode/plugins/agent-comms/` (respects `XDG_CONFIG_HOME`; override
with `--opencode-dir`). It points to this checkout. Rerun the installer after
moving the checkout on a machine; the checked-in plugin contains no absolute
dotfiles path. OpenCode discovers the loader automatically. Use `/reload` to
reload plugins and the revised Naia instructions in an already-open session.

The server plugin injects instructions through the V2 session `context` hook,
before the **first agent-loop model request**, including new sessions created
inside the TUI. Empty sessions and title-generation requests do not consume IDs.
Each tool continuation and post-compaction agent-loop request receives the same
identity block. Concurrent initialization is coalesced. A bounded 128-session
cache avoids spawning Python for every tool continuation; reloading/eviction
resolves the persisted assignment again instead of drawing another identity.
Reload after changing the communication template or identity metadata.

The plugin uses `python3` on POSIX and `py -3` on Windows. Set
`AGENT_COMMS_PYTHON` to an executable path (not a shell command) to override it, or
configure the plugin's `python` option. It invokes the helper as an argument
vector, never interpolating session IDs into shell commands. Identity setup
failures are advisory, logged once, and retried no more often than every 30
seconds for that session. The plugin runs on the **OpenCode server**. The v1
integration assumes the listener and server are on the same machine.

### Codex

The installer registers this directory as the `agent-comms-local` local
marketplace and installs `agent-comms@agent-comms-local` using the official Codex
CLI. The adapter is packaged under `plugins/codex/`; its `SessionStart` hook
returns `hookSpecificOutput.additionalContext` for `startup`, `resume`, `clear`,
`compact`, and `fork` events. It resolves the canonical helper through the
installed launcher, even when Codex runs a cached copy of the plugin.

**Required manual step:** in Codex, open `/hooks`, review the agent-comms
SessionStart command, and trust it. Installation/enabling does not imply trust.
Untrusted hooks are skipped. No approval bypass is installed or recommended.
Start/resume a conversation after reviewing the hook. Existing Beads and tuios
hooks are left untouched. Helper failures emit only an advisory diagnostic, not
model-visible raw errors, and never block the main task. Scripts and dependencies
must be available locally; this is not a cloud-orchestrated Work integration.

### Shared session ownership and messaging policy

Mappings live in private `state/sessions/`, keyed by a hash of **harness +
session ID**, with a separate per-session OS lock. Only the first lookup draws
from the normal locked random bag. Resumes, compaction, process restarts, plugin
reloads, and concurrent callbacks reuse the ID. Forked sessions have new session
IDs and receive their own draw. Child-agent-specific Codex hooks are not installed
in this version; the adapter targets SessionStart events.

Metadata comes from the current config. If an assigned identity is removed from
the roster, the next lookup reassigns it; a corrupt mapping is an explicit error
rather than repeatedly consuming bag entries. There is no lease or mapping
expiration yet. Allocation and mapping are two atomic writes: a crash between
them can consume an unused bag entry, but cannot expose a partial mapping.

[communication-instructions.md](communication-instructions.md) is the single
template used by both adapters. It carries over the former `inform_user` rhythm:
open longer tasks, send useful progress, and **close every turn immediately
before the final text response**, even for short answers. It preserves the
native-voice exception and Claude Code readability guidance, requests editor
cards before the closing message, and prohibits duplicate `nvim_inform_user`
announcements. It also covers safe quoting, secrets, existing permissions, and
continuing without retry spam when the listener is offline.

Both adapters explicitly require the configured personality in **only** the
`--msg` and `--tts` communications text. Agents **must not** adopt it in standard
responses, explanations, code, or other tool interactions. Speech examples and
the injected current model name reflect the Naia-default Turbo model.

The old voice directives were removed from `nvim/bin/agent-instructions.md` and
`nvim/bin/agent-reminder.md`; Neovim display/navigation instructions remain.
Existing conversations can retain older instructions until reloaded/resumed.
Claude Code does not have an agent-comms startup adapter in this version; the
shared template keeps its readability rule for reuse, not automatic installation.

Additional tests:

```sh
node --test agent-comms/plugins/opencode/index.test.mjs
AGENT_COMMS_TEST_OPENCODE=1 python3 -m unittest discover -s agent-comms -p test_opencode_live.py -v
```

The opt-in OpenCode test dispatches through the installed V2 binary to a local
fake LLM with isolated HOME/config/state; it makes no external model calls.

### Codex localhost sandbox restrictions

Codex shell commands with `workspace-write` and `network_access = false` cannot
connect to the listener's localhost TCP port, even when the listener is healthy.
The CLI reports this as a permissions/sandbox block, not an offline listener.
Restarting the listener does not fix that restriction. SessionStart hooks can
still allocate an identity because allocation only needs local files.

Use Codex's normal approval flow for any additional access. Do not automatically
enable general networking, bypass the sandbox, auto-approve commands, or silently
switch transports. A separately authorized narrow messaging tool is another
possible integration, but is not installed by this version.
