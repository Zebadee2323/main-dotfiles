# agent-comms — v1 specification

## Purpose

Provide shared identities and a single, per-user message listener for coding
agents. Agents draw an identity, send messages under that identity, and see those
messages presented sequentially in a terminal or the tuios dock.

The program lives in `tuios/agent-comms/` in this dotfiles repository. This
specification captures the agreed design before implementation.

Later extensions are implemented and documented in README.md: durable `history`,
separate `send --tts` speech text, watcher-side configurable local Phonon 31m or
ElevenLabs speech (Naia-default `eleven_turbo_v2_5`), backend-specific injected
speech guidance, synchronized caption chunks and attention flashes,
explicit checked spatial prefixes returned by `context`, pane-stable identity ownership,
delivery VFX, listener-wide live `audio` toggle/timed mute, and exact-owned
`[NAME] ` pane-name prefixes applied by `context`. Personalities and the separate
`prefix` command have been removed.
Those extensions supersede the original v1
voice/history exclusions below. The current wire protocol is version 2.
State snapshots include a public listener generation, distinct from the auth
token; listener generation changes do not reload watchers. Watchers poll an
atomic runtime revision file every 250 ms, even while disconnected. An explicit
`restart watchers` changes that revision, causing watchers to close playback and
worker resources and re-execute with preserved arguments. `restart all` first
restarts the installed listener service, then signals watchers (only on service
success). `service restart` affects only the listener. The tuios palette exposes
all three scopes. None clears pane identities or history.
`context` now returns the caller's ID, name, and exact introduction or empty
prefix, resolving ownership from the tuios environment only (no harness/session
identity-selection flags). Outside tuios, lookup fails clearly. Agents must insert
it themselves in both display and TTS text. `send` rechecks context and rejects
mismatches without queueing or rewriting the message. This supersedes the earlier
automatic-introduction behavior.

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
- Keep the editable identity config in `agent-comms/config.toml`, resolved beside
  the program on every platform. Keep state/runtime data in native per-user
  locations outside the source tree, with overrides for isolated tests.
- Dock output supports ANSI SGR colors and styling.

## Commands

### `agent-comms context`

Resolve the caller pane's stable identity and return JSON with `id`, `name`,
and `prefix`. Agents call this initially in a separate tool call, then reuse
these details in the same pane until a send reports an identity/prefix mismatch.
Never inherit identity details from another pane or conversation.

The CLI performs a live local pane check, then requests identity allocation via
the running listener's authenticated `context` RPC. It forwards only allowlisted
tuios addressing variables. The listener verifies the pane with that environment,
persists the binding, and updates the pane title. Routine `context` and `send`
commands perform no client-side state writes. Send verification reads an atomic
binding snapshot without a write lock. An older listener requires a restart;
there is no fallback to writing application state from inside the agent sandbox.

Allocation uses the original Tetris-style bag internally, but repeated lookup
does not draw a new identity. The following describes the internal allocator:

- Diagnostics belong on stderr; successful lookup stdout is JSON.
- The configuration bag contains every configured identity ID once.
- The transient bag is a persisted, shuffled copy of those IDs.
- A draw removes and returns the ID at the front of the transient bag.
- If the bag is empty, refill it from configuration and shuffle before drawing.
- Each identity appears exactly once per cycle. A repeat across cycle boundaries
  is permitted, as with a Tetris bag.
- Persist remaining IDs between CLI calls. Protect the complete read/modify/write
  operation with a process-safe lock so concurrent callers cannot consume the
  same bag entry.

### `agent-comms context --refill`

Force a fresh shuffle of the complete configuration bag, replacing any remaining
transient entries. **Do not draw or print an ID.** Use the same lock as normal
draws.

### `agent-comms context --clear-all`

Explicit local administrative reset of all pane bindings in the selected state
directory, across all daemons and sessions. Acquire the same lock as allocation
and atomically replace bindings and scan timestamps with empty tables. Print
`{"cleared": N}` with the number of released bindings; repeated resets return zero.
Do not allocate identities, refill the bag, edit config, delete history, change
titles, or cancel queued announcements. Existing listeners observe the reset
without restarting; each pane must look up `context` before its next send (a new
draw may return the same identity). Works outside tuios and offline, without
loading config, but requires state write access. Corrupt mappings fail explicitly.
Mutually exclusive with `--clear` and `--refill`.

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
- Show the identity's configured `name` before the message: `Name: message`.
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

### `agent-comms send --id ID --msg MESSAGE`

Submit a text message using the caller pane's internally resolved identity.

- Resolve the pane binding and validate the message. Unbound panes fail clearly.
  Required `--id` asserts the expected identity, never selects it. A mismatch
  rejects the message before networking and instructs the agent to run
  `agent-comms context` for an up-to-date ID, name, and prefix.
- Connect to the global listener using discovery and authentication below.
- Return success only when the listener acknowledges queue acceptance. Success
  does not mean the message has already appeared on screen.
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

## Identity configuration and bag state

Use TOML keyed by globally unique stable identity IDs, either legacy
`[identities.ID]` or `[identity_groups.GROUP.identities.ID]`. A top-level
`active_groups` array selects the union eligible for bag draws and voice review.
Omitting it includes all identities. Invalid/empty/duplicate group selections fail
closed; a membership change invalidates the bag. Existing pane bindings remain
stable until cleared or their identity is removed. Each identity has:

- `name`: the human-readable display name.
- `eleven_labs_voice_id`: voice metadata.
- `phonon_voice`: local Phonon catalog name; blank/missing disables Phonon speech
  for this identity. `[speech].mode` selects `phonon`, `pocket_tts`, or `elevenlabs` (legacy default).
  `[speech.phonon]` configures the external `ptts` executable and 31m model repo.
  Runtime dependencies and cached weights are stored outside the source tree.
- `phonon_voice_approved`: Phonon-only review approval (default false), independent
  of ElevenLabs `voice_approved`. `voice-review` uses the configured provider;
  `--all` includes approved voices, `s` skips without saving any decision, and
  `r` replays the existing sample without synthesizing again.
- `pocket_tts_voice`: Pocket preset name or permitted local reference voice path.
  Local WAV and exported `.safetensors` paths may be absolute, home-relative, or
  relative to the configuration file's directory (for example `voices/alice.wav`).
  Config loading resolves local paths for both review and announcement playback
  without rewriting the source TOML; preset names and URLs remain unchanged.
  `[speech.pocket_tts]` configures the external executable and language.
  `pocket_tts_voice_approved` is its independent approval flag (default false).
  Each watcher lazily owns a private Pocket model worker, started only after a
  successful speech claim. Voice conditioning states are cached strictly on first
  use, not preloaded from the roster. Model/cache persist until watcher exit or
  interrupted/failed synthesis; pipes serialize requests without a network service.
- `elevenlabs_speed` / `phonon_speed` / `pocket_tts_speed`: independent global or identity multipliers
  (0.7–1.2). Identity overrides global; provider-specific overrides legacy `speed`
  within each scope. Used by both watcher playback and voice review.

Illustrative structure, not a finalized identity roster:

```toml
display_seconds = 5

[identities.naia]
name = "Naia"
eleven_labs_voice_id = ""
```

Names and voices are identity metadata. Personalities have been removed; use
ordinary communication style. Optional watcher speech is documented in README.md.

The default configuration lives beside the program in `agent-comms/config.toml`
(updated by request after the initial implementation). Flags/environment can
override it. Configuration is the authoritative bag of possible IDs; a separate locked JSON
state file stores the remaining shuffled IDs. Runtime state must not be written
into the checked-in source/configuration example.

Pane bindings reserve IDs, so different retained pane bindings cannot silently
share an identity. `send` resolves the current pane binding internally. Ownership is
persisted by stable daemon namespace + pane UUID under a shared OS lock. Confirmed
missing panes are pruned after a seven-day grace period; failed/incomplete listings
never prune. See README.md for lifecycle and migration details. This is attribution,
not authentication; the local connection token provides transport authentication.

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
     "protocol_version": 1,
     "port": 51432,
     "token": "<random secret>"
   }
   ```

The port is illustrative. No fixed port or manual port configuration is needed.

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
- Handle interrupted bag writes and discovery writes safely; atomic replacement
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

- Initially show a colored identity name and current message, with a quiet idle
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

- Every configured identity appears once per full bag cycle, with automatic
  refill, persistent remaining entries, and refill-only `--refill` behavior.
- Concurrent ID calls coordinate safely.
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

- Default identity roster and actual voice metadata.
- Exact configuration/state paths, override flags/environment variables, and
  install/launcher mechanism on each platform.
- Transport framing, payload limits, queue limits, timeouts, and retry backoff.
- Policy for changed identity configuration or corrupt transient bag state.
- How the standalone server is launched/supervised independently of dock
  watchers, including optional startup automation.
- Exact dock placement, colors, width limits, and long-message presentation.
