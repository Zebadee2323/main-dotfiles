# agent-comms — v1 specification

## Purpose

Provide generated agent names and a single, per-user message listener for coding
agents. Each agent is assigned a name and a gender-matched voice, sends messages
under that name, and sees those messages presented sequentially in a terminal or
the tuios dock.

The program lives in `tuios/agent-comms/` in this dotfiles repository. This
specification captures the agreed design before implementation.

Later extensions are implemented and documented in README.md: durable `history`,
separate `send --tts` speech text, watcher-side configurable local Phonon 31m or
ElevenLabs speech (Naia-default `eleven_turbo_v2_5`), backend-specific injected
speech guidance, synchronized caption chunks and attention flashes,
explicit checked spatial prefixes returned by `context`, pane-stable name ownership,
delivery VFX, listener-wide live `audio` toggle/timed mute, and exact-owned
`[NAME] ` pane-name prefixes applied by `context`. Personalities and the separate
`prefix` command have been removed.
Those extensions supersede the original v1
voice/history exclusions below. The current wire protocol is version 3; mixed
client/listener/watcher versions fail with restart guidance (`agent-comms restart all`).
State snapshots include a public listener generation, distinct from the auth
token; listener generation changes do not reload watchers. Watchers poll an
atomic runtime revision file every 250 ms, even while disconnected. An explicit
`restart watchers` changes that revision, causing watchers to close playback and
worker resources and re-execute with preserved arguments. `restart all` first
restarts the installed listener service, then signals watchers (only on service
success). `service restart` affects only the listener. The tuios palette exposes
all three scopes. None clears name bindings or history.
`context` now returns the caller's generated name, reset epoch, and exact
introduction or empty prefix, resolving ownership from the tuios environment or
persistent parent shell (no harness/session identity-selection flags). Outside
tuios, the listener derives the shell from the caller process ancestry; no tuios
executable is needed. Agents must insert the prefix themselves in both display and
TTS text. `send` rechecks name, epoch, and context and rejects mismatches without
queueing or rewriting the message. Missing bindings also reject the send and
return fresh context via the listener; they do not trigger an automatic resend.
Corrupt, unsupported, or legacy allocation state does not trigger recovery or
allocation. This supersedes the earlier automatic-introduction behavior.

The static identity roster (opaque `ag_...` IDs with fixed display names) has
been replaced by generated names plus voice profiles, an explicit reset epoch, and
a silent conversation view; see "Names, voice profiles, and allocation state" below.
The refactor design is in `refactor-spec.md`.

## Technology and platforms

- Python 3.11 or newer, standard library only; no pip dependencies.
- `argparse` for the CLI, `tomllib` for TOML configuration, JSON for transient
  state and transport data, and `asyncio` for the server and live subscriptions.
- Support macOS, Linux, and Windows from the outset. Windows testing is not
  required at this stage; do not depend on Unix-only behavior without a Windows
  implementation.
- Use localhost TCP, not Unix-domain sockets, for communications.
- Use platform-specific OS-backed file-lock helpers: `fcntl` on macOS/Linux and
  `msvcrt` on Windows. Locks must coordinate separate processes and release when
  a process exits or crashes.
- Keep the editable config in `agent-comms/config.toml` (and its `names.toml`), resolved beside
  the program on every platform. Keep state/runtime data in native per-user
  locations outside the source tree, with overrides for isolated tests.
- Dock output supports ANSI SGR colors and styling.

## Commands

### `agent-comms context`

Resolve the caller pane or shell's stable assignment and return JSON with `name`,
`epoch`, and `prefix` (plus `id`, a temporary compatibility alias equal to `name`):

```json
{"id":"Alice2","name":"Alice2","epoch":"3f9c0a7d1b2e4c68","prefix":"Alice2 here, to your top-right."}
```

Agents call this initially in a separate tool call, then reuse these details in
the same pane until a send reports a name/epoch/prefix mismatch. Never inherit
identity details from another pane or conversation.

The CLI performs a live local pane check, then requests allocation via
the running listener's authenticated `context` RPC. It forwards only allowlisted
tuios addressing variables. The listener verifies the pane with that environment,
persists the binding, and updates the pane title. Routine `context` and `send`
commands perform no client-side state writes. Pane send verification reads the
atomically replaced allocation document without a lock.
Standalone clients forward their process ID; the listener discovers the nearest
persistent ancestor shell, skipping transient command shells, and keys ownership
by host, PID, and process creation time. Standalone sends use a read-only
`shell-binding` RPC. Discovery advertises `shell_identity: true`. Shell process
inspection uses POSIX ps or Windows PowerShell CIM in the listener; clients need
no process-query permission. Without a shell ancestor, lookup fails explicitly.
Tuios remains authoritative when its environment is present, including errors.
Standalone prefixes are empty and pane-title updates are skipped. An older listener requires a restart;
there is no fallback to writing application state from inside the agent sandbox.

Repeated lookups return the existing binding, including after listener or machine
restarts; only a missing binding allocates. Allocation is described under
"Names, voice profiles, and allocation state". Diagnostics belong on stderr;
successful lookup stdout is JSON.

### `agent-comms context --clear`

Release only the caller's binding and print `{"cleared": bool, "name": ...}`
(`id` mirrors `name`). The name is not returned to the bag; the next `context`
allocates a fresh name.

### `agent-comms context --refill` (retired)

Fails with guidance. Names refill automatically when the bag is exhausted; a
premature refill could reuse names within a generation. Use `bags reset --confirm`
for an explicit restart at generation 0.

### `agent-comms context --clear-all`

Explicit local administrative clear of all pane and shell bindings in the selected
state directory, across all daemons and sessions. Acquire the allocation lock
and atomically replace bindings and scan timestamps with empty tables. Print
`{"cleared": N}` with the number of released bindings; repeated clears return zero.
Do not allocate, refill or rewind the bags, change the epoch, edit config, delete
history, change titles, or cancel queued announcements. Issued names stay spent.
Existing listeners observe the change without restarting; each agent must look up
`context` before its next send and receives a fresh name. Works outside tuios and
offline, without loading config, but requires state write access. Corrupt or legacy
state fails explicitly. Mutually exclusive with `--clear`.

### `agent-comms bags status` / `agent-comms bags reset --confirm`

`status` prints the epoch, generation, remaining name and per-gender voice bag
sizes, catalog size, binding count, and bound names as JSON (or
`initialized: false`, with `legacy: true` and guidance for legacy state).

`reset --confirm` is the only operation that changes the epoch. Under the
allocation lock it starts a new epoch at generation 0, refills all bags from
current config, and clears every binding in one write. It preserves history and
voice approvals/rejections; already-accepted messages keep their recorded name and
voice. Legacy `pane-identities.json`/`bag.json` and corrupt `allocation.json` are
moved into `state/legacy-backup-*` first. Without `--confirm` it refuses. Ordinary
service start/restart never resets.

### `agent-comms serve`

Run the single global listener and own the timed message queue.

- Global means **one server per OS user**, shared across repositories, terminals,
  tuios sessions, and workspaces—not one server per workspace and not a
  machine-wide service shared between users.
- Enforce the singleton with an OS-backed lock, independently of discovery-file
  existence. A second active instance fails clearly.
- Receive and validate messages and notify subscribed watchers.
- Accept messages into a FIFO queue. Acknowledge a send only after acceptance.
- Display one message at a time for a configurable duration, default **5
  seconds**, then remove it and display the next queued message.
- Show the sender's generated `name` before the message: `Name: message`.
- When no messages remain, show a quiet idle indicator.
- Retain a standalone terminal display, wrapping messages to the terminal width.
- Keep the queue in memory. Restarting the server discards active and queued
  messages; durable delivery is not part of v1.
- The server lifecycle is independent of dock watchers. Closing a tuios client
  must not terminate the global listener merely because its watcher exits.

### `agent-comms service ACTION` (macOS / experimental Windows)

- Provide `install`, `start`, `stop`, `restart`, `status`, `logs`, and `uninstall`
  through a per-user launchd LaunchAgent, never an agent-owned background process.
- Install is explicit. `start --if-installed` skips an absent installation and is
  suitable for tuios's client-side `after-attach` hook. Repeated starts do not
  interrupt a running listener. Serialize mutations and preserve the server lock.
- Wait for an authenticated `ping` response whose PID matches launchd before
  reporting readiness. Do not silently terminate unmanaged listeners.
- Start at login, restart on exit with throttling, and unload on explicit stop.
  Uninstall preserves state and logs. No root privileges or credential copying.
- Pin executable/config/state/runtime paths. Refuse to manage an installation
  with different paths or overwrite unrelated definitions.
- Persist rotating lifecycle logs with signals and unexpected failures, excluding
  message contents and authentication tokens. Capture early failures separately.
- On Windows, use a SID-scoped Task Scheduler task with an interactive-token,
  least-privilege logon trigger. No admin service or stored password. Ignore new
  instances, disable the execution time limit and battery restrictions, and retry
  failed exits at the scheduler's minimum one-minute interval up to 255 times.
- Run the listener in the action PID via a small Python runner, capture early
  stderr, and remove caller identity/credential variables. Validate readiness
  with an authenticated per-installation ID, not a Task Scheduler engine PID.
- Windows stop disables the task before terminating its instances, waits for
  them to end, and does not promise POSIX signal cleanup. Start re-enables logon
  startup. Never use process-name kills or silently overwrite unrelated tasks.
- Transfer all task data as JSON to fixed PowerShell COM code, use locale-neutral
  status, XML escaping and Windows argv quoting, and preserve state on uninstall.
  Keep Windows support marked unvalidated until native smoke tests pass.
- Other platforms retain foreground `serve`; unsupported service commands explain
  the limitation (`start --if-installed` safely skips).

### `agent-comms send --name NAME --epoch EPOCH --msg MESSAGE`

Submit a text message using the caller's internally resolved binding.

- Resolve the pane or shell binding and validate the message. Unbound callers fail
  clearly. `--name` and `--epoch` assert the expected assignment, never select it.
  `--id` is a deprecated alias for `--name` (a stderr note is printed; both
  together are a usage error). A missing or mismatched name or epoch rejects the
  message and prints fresh context JSON (`id`, `name`, `epoch`, `prefix`) on stderr.
- The listener re-verifies name and epoch against the owner's binding under the
  allocation lock (mismatch → `context_mismatch` error frame), builds the record
  from the binding's embedded voice snapshot (never live config), and assigns a
  unique message ID.
- Optional display metadata for the conversation view: `--display-recipient`,
  `--role`, `--kind`, `--emotion`, `--reply-to` (see README.md). It never routes or
  delivers anything and cannot change the verified sender.
- Connect to the global listener using discovery and authentication below.
- Return success only when the listener acknowledges queue acceptance; print the
  estimate and `Message ID: <id>`, plus `agent-comms: warning: ...` lines on stderr.
  Success does not mean the message has already appeared on screen.
- If the listener is offline, exit nonzero with a clear diagnostic. Do not
  automatically start a server or save messages for later delivery.

### `agent-comms watch`

Subscribe to the listener's current display state and produce live single-line
updates for a tuios dock component.

- Stay running and print a complete replacement line whenever the current
  message changes. Flush updates promptly.
- Immediately receive the current state when subscribing, rather than waiting
  for the next message.
- Each watcher observes the same server-owned queue and display timing. Watchers
  do not dequeue messages, restart timers, or compete to receive messages.
- Multiple tuios clients can each run a watcher without starting more servers.
- If offline, display a disconnected indicator and retry. Reread discovery data
  on reconnect so a server restart with a new port/token is recoverable.
- Keep diagnostics off stdout; stdout is the dock's display stream.
- Flatten multiline messages for the dock while preserving terminal wrapping in
  the standalone server display.
- `watch --view convo` is a separate, always-silent, pane-sized view (it
  requires a terminal). It never claims speech; it follows the listener's shared
  presentation lifecycle (`start`, `display`, `end` events in each snapshot's
  `lifecycle` object), deduplicates by timeline/sequence, and resynchronizes to
  the current snapshot after a listener restart. Late joiners see the current
  message only; history is not replayed.

### `agent-comms history`

Print retained accepted messages, even offline. `--name` (generated name),
`--epoch`, `--session`, and deprecated `--id` filter within the retained window.
`--watch [--refresh-time SECONDS]` redraws in place when content or size changes;
it requires a terminal and rejects `--json`/`--raw`. Watching never speaks or sends.

## Names, voice profiles, and allocation state

The generated agent name is the only public agent identifier. Names and voices
are configured independently and paired by gender at allocation.

- `names_file` (relative to the config) points to a catalog with only `male` and
  `female` arrays. Names are 1–32 ASCII letters starting uppercase, unique
  case-insensitively across both arrays; `user` is reserved. The catalog must be
  nonempty, and every represented gender needs an active voice profile.
- Voice profiles live in `[voice_groups.GROUP.voices.PROFILE_ID]`. Profile IDs are
  unique across groups and are not agent identifiers. Each profile requires
  `gender = "male"|"female"` and may contain only voice settings (provider voice
  fields, approval flags, speed overrides); `name` is rejected. Legacy
  `[identities]`/`[identity_groups]` tables are rejected with conversion guidance.
  `active_groups` (default: all groups) selects profiles for new assignments and
  voice review only.
- Name bag: one global generation. Generation 0 shuffles the whole catalog and
  issues each base name once (`Alice`). When empty, the next allocation increments
  the generation and refills from the current catalog: generation g > 0 issues
  `Alice{g+1}` (`Alice2`, `Alice3`, ...). Catalog edits apply on the next refill.
- Voice bags: one shuffled queue per gender over eligible profiles, refilled
  independently. A name's gender selects the queue. Many agents may share a
  profile; voice cycles never cap concurrent agents or advance the generation.
  Active-group changes reconcile the queues for future draws only.
- A binding records `name`, `base_name`, `gender`, `name_generation`,
  `voice_profile_id` (provenance only), and a deep-copied `voice_profile`
  snapshot with absolute voice paths. Delivery uses the snapshot; editing, moving,
  or deleting the source profile does not affect the binding until it is cleared.
- Clearing or pruning a binding never returns its name to the bag.

Profile fields (all optional except `gender`):

- `eleven_labs_voice_id` / `voice_approved`: ElevenLabs voice and review approval.
- `phonon_voice` / `phonon_voice_approved`: local Phonon catalog name; blank/missing
  disables Phonon speech for this profile. `[speech].mode` selects `phonon`,
  `pocket_tts`, or `elevenlabs` (legacy default). `[speech.phonon]` configures the
  external `ptts` executable and 31m model repo; runtime dependencies and cached
  weights are stored outside the source tree.
- `pocket_tts_voice` / `pocket_tts_voice_approved`: Pocket preset name or permitted
  local reference voice path. Local WAV and exported `.safetensors` paths may be
  absolute, home-relative, or relative to the configuration file's directory; they
  are resolved at load (and stored absolute in snapshots) without rewriting the
  TOML. Each watcher lazily owns a private Pocket model worker, started only after
  a successful speech claim; voice states are cached on first use, not preloaded.
- `elevenlabs_speed` / `phonon_speed` / `pocket_tts_speed` (and legacy `speed`):
  0.7–1.2. Profile overrides global; provider-specific overrides legacy `speed`
  within each scope. Used by both watcher playback and voice review.

Approvals are profile-scoped. `voice-review` audits active profiles with the
configured provider (never generated agents); `--all` includes approved voices,
`s` skips without saving, and `r` replays without synthesizing again.

Illustrative structure:

```toml
names_file = "names.toml"
active_groups = ["phonon"]

[voice_groups.phonon.voices.voice_juniper]
gender = "female"
phonon_voice = "Juniper"
eleven_labs_voice_id = ""
```

State is a single versioned document, `STATE/allocation.json`, guarded by
`allocation.lock` and written by atomic replacement. It holds the epoch, name bag,
voice bags, bindings, scan times, and retired title prefixes. Allocation, clear,
prune, reset, title updates, and message acceptance serialize through that lock;
a name, voice, and binding are committed together. Send paths read it without
locks, directory creation, or permission changes. Corrupt, unsupported, or
inconsistent state fails closed with `bags reset --confirm` guidance; it is never
silently recreated. Pre-refactor `pane-identities.json`/`bag.json` without
`allocation.json` is legacy state and fails until an explicit reset migrates it.

The epoch is an opaque, state-directory-wide value that changes only on
`bags reset --confirm` (not on restarts or refills). Name uniqueness is scoped to
one state directory and one epoch.

Optional watcher speech and provider settings are documented in README.md.
Personalities have been removed; use ordinary communication style. Runtime state
must not be written into the checked-in source/configuration.

Ownership is persisted by SHA-256 of `TUIOS_PANE_ID` alone (or host + shell PID
+ process start time outside tuios) inside the allocation document. Connection
environment is routing/cleanup metadata, never part of the pane identity key.
Checked context lookups migrate unambiguous old daemon-scoped bindings; conflicts
require an explicit per-pane clear rather than selecting an identity. Confirmed missing panes and
exited shells are pruned after a seven-day grace period; failed/incomplete
listings never prune. This is attribution, not authentication; the local
connection token provides transport authentication. Clear commands apply to both
types of ownership.

## Server discovery and authentication

### Locations

All default clients for the same OS user resolve the same runtime location,
independent of their current directory or tuios session.

- macOS/Linux: a per-user runtime/state directory, respecting applicable XDG
  settings and using a user-private fallback when necessary.
- Windows: `%LOCALAPPDATA%\agent-comms\run\` for runtime discovery/lock data.
- Provide an explicit override for isolated development/test instances. Server
  and clients using that override must resolve the same location.
- WSL and native Windows are initially separate environments. Cross-environment
  discovery is not promised.

Exact override names and Unix fallback paths remain implementation details and
must be documented with the implementation.

### Startup

1. Acquire the singleton lock for the selected runtime location.
2. Bind TCP to **`127.0.0.1` only**, selecting an available ephemeral port.
3. Generate a cryptographically random authentication token.
4. Atomically publish a discovery JSON file after the listener is ready:

   ```json
   {
     "protocol_version": 3,
     "name_identity": true,
     "conversation_view": true,
     "presentation_lifecycle": true,
     "port": 51432,
     "token": "<random secret>"
   }
   ```

The port is illustrative. No fixed port or manual port configuration is needed.
Discovery also carries the other capability flags (`prefix_policy`,
`spatial_prefix`, `audio_control`, `pane_identity`, `pane_identity_clear`,
`shell_identity`). Clients refuse a listener whose protocol version differs or
that lacks a capability they need, with restart guidance.

### Client connection

`send` and `watch` read the discovery file, connect to localhost at its advertised
port, and authenticate with the token. Validate discovery/protocol data and fail
clearly for unsupported protocol versions or authentication failures. Do not
trust discovery-file presence as proof that a listener is alive.

The discovery file contains a secret. Store it in a user-private directory and
apply restrictive permissions where supported, taking Windows access behavior
into account. Do not expose tokens in normal CLI output, dock text, or logs.

### Shutdown and recovery

- A crash releases the OS lock even if discovery data remains on disk.
- A new server that acquires the lock replaces stale discovery information with
  its own port/token.
- Watchers reconnect using freshly read discovery data.
- Clean shutdown should remove the departing server's discovery data safely;
  avoid deleting data published by a replacement server.
- Handle interrupted allocation-state writes and discovery writes safely; atomic replacement
  must not compromise the separate lock's effectiveness.

## tuios integration

### Chosen surface: bottom dock component

The initial request was a short, full-width bottom process rail that stays
visible across session/workspace views. The installed tuios 0.11.0 supports
left/right terminal session rails, not an arbitrary full-width bottom process
rail. The agreed v1 direction is a **single-line dock component** instead.

The dock runs `agent-comms watch`; tuios renders the text that watcher supplies.
It does **not** embed the watcher's terminal screen or host an interactive TUI.

Illustrative integration, to merge with existing dock tables rather than replace
unrelated components:

```toml
[dock.custom.agent-comms]
command = "<installed agent-comms command> watch"
refresh = "push"
```

Add `custom/agent-comms` to an appropriate `[dock]` component list. The existing
configuration already places the dock at the bottom. Keep the global server
separate from the client-owned watcher; do not configure `serve` as the push
component.

### Appearance and capabilities

- Initially show a colored agent name and current message, with a quiet idle
  or disconnected indicator when appropriate.
- Single-line ASCII graphics are supported: faces, badges, and progress bars.
  For example:

  ```text
  [o_o] Naia: Checking the build... [###--]
  ```

- tuios preserves ANSI SGR colors and styling, including RGB colors, in custom
  component output. Names, graphics, and message text can have different styles.
- A simple animation can emit a complete new line for each frame. No animation
  or elaborate graphics are required for the initial display.
- Cursor movement, screen-clearing sequences, and other non-SGR controls are
  stripped by tuios. Do not rely on carriage-return redraws or terminal cursor
  positioning; newline-delimited replacement frames are the contract.
- This is one line, not a multi-row panel. Other dock components share the bar;
  full-width allocation is **not guaranteed**. Handle long messages with a
  compact/truncated presentation rather than assuming unlimited space.
- Treat incoming message text as display data, not trusted terminal control
  sequences; styling should be owned by the renderer.
- The dock is outside the workspace pane layout, so its component can remain
  present across central session/workspace changes while the dock is visible.
- Each tuios client runs its own watcher locally. A client detaching ends its
  watcher, not the global listener. Reattaching creates a new watcher that
  receives current state.
- The program should account for Windows, but actual Windows verification of
  tuios dock behavior is deferred.

### Reference checked during design

The installed tuios version was 0.11.0, commit
`3638a6f2093edfa7396b6233515786de4dbda683`.

- [Custom dock contract and SGR support](https://github.com/Gaurav-Gosain/tuios/blob/3638a6f2093edfa7396b6233515786de4dbda683/examples/dock/README.md)
- [Sidebar session rails and limitations](https://github.com/Gaurav-Gosain/tuios/blob/3638a6f2093edfa7396b6233515786de4dbda683/docs/CONFIGURATION.md#show-a-daemon-session-in-each-rail)

## Validation goals

Implementation should verify, without requiring Windows execution for now:

- Each base name appears once per generation, later generations add numeric
  suffixes, every assigned name/voice pair has matching gender, and voice cycling
  never caps concurrent agents. Bags, generation, and bindings survive restarts.
- Concurrent allocations never issue duplicate names or partial state; clearing
  and pruning never recycle names; corrupt or legacy state fails closed.
- Only `bags reset --confirm` changes the epoch; pre-reset contexts are rejected
  even when the same owner receives the same name again.
- Only one listener can run for a selected per-user runtime location.
- Discovery, authentication, stale-file recovery, and restart/reconnect behavior
  work without a fixed port.
- Messages are named, queued FIFO, and expire after the configured duration.
- Multiple watchers receive the same current state, including on initial attach.
- Offline sends fail clearly; offline watchers remain alive and reconnect.
- Display output is safe, single-line in watch mode, and suitable for colored
  tuios push updates.
- The final tuios configuration preserves existing unrelated settings and shows
  the watcher in the bottom dock.

## Out of scope for v1

- Speech synthesis or ElevenLabs API integration.
- Exclusive identity reservations and leases.
- Durable message queues or offline send buffering.
- Remote/network listeners, cross-user messaging, or shared native Windows/WSL
  discovery.
- A custom tuios core extension, arbitrary new bottom rail, embedded dock
  terminal, or multi-line dock UI.
- Mandatory animation, graphical sophistication, or Windows execution testing.

## Details to settle during implementation

The design above is agreed; the following have not yet been finalized:

- Default name catalog and actual voice metadata.
- Exact configuration/state paths, override flags/environment variables, and
  install/launcher mechanism on each platform.
- Transport framing, payload limits, queue limits, timeouts, and retry backoff.
- Policy for changed name/voice configuration or corrupt allocation state.
- How the standalone server is launched/supervised independently of dock
  watchers, including optional startup automation.
- Exact dock placement, colors, width limits, and long-message presentation.
