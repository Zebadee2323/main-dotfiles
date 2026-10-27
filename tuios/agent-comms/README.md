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

For a persistent macOS listener, prefer the per-user launchd service below.
`serve` is a foreground/debugging command; do not use an agent's background
shell tool to host a global listener.

In another tuios pane:

```sh
# Inspect the JSON first, then use its ID and exact name/prefix in a separate call.
./agent-comms context
# Replace the prefix placeholders below (omit an empty prefix).
./agent-comms send --id RETURNED_ID --msg "RETURNED_PREFIX Hello from the agent dock!" --tts "RETURNED_PREFIX Hello from the agent dock!"
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
$lookup = .\agent-comms.cmd context | ConvertFrom-Json
if ($LASTEXITCODE -ne 0) { throw "Could not resolve identity" }
$prefix = $lookup.prefix
$message = if ($prefix) { "$prefix Hello from Windows!" } else { "Hello from Windows!" }
.\agent-comms.cmd send --id $lookup.id --msg $message --tts $message
```

## Commands

| Command | Behavior |
| --- | --- |
| `context` | Resolve pane identity; print `id`, `name`, and `prefix` as JSON. Reuse until a send returns fresh context on mismatch. |
| `context --refill` | Replace remaining entries with a new shuffled bag; no draw/output. |
| `context --clear` | Release only the caller pane's mapped ID; print `cleared` and the previous `id` as JSON. No replacement is allocated. |
| `context --clear-all` | Release every pane mapping in the selected state directory; print the number cleared as JSON. Works outside tuios and with the listener offline. |
| `voice-review` | Audition non-approved voices using the configured TTS mode; approve, reject, skip unchanged, replay, or quit. |
| `voice-review --all` | Audition every configured voice, including already-approved ones. |
| `serve` | Own the singleton listener, timed FIFO queue, and terminal display. |
| `serve --quiet` | Same listener without terminal display. |
| `service install/start/stop/restart/status/logs/uninstall` | Manage the per-user macOS LaunchAgent or experimental Windows scheduled task; see below. |
| `send --id ID --msg TEXT --tts SPEECH` | Verify the expected ID against the pane binding, save and queue text; print approximate total time until completion. |
| `history` | Show readable, wrapped history with timestamps and sender/session headings, even offline. Colors appear only in a terminal (respecting `NO_COLOR` / `--no-color`). |
| `history --raw` | Print unstyled, one-line timestamp/name/message records. |
| `history --json` | Print full machine-readable records, including speech text. Mutually exclusive with `--raw`. |
| `history --limit 5 --id nova --json` | Filter/limit retained history and optionally include complete metadata and speech text. |
| `history --session work` | Filter by the sender's tuios session name. |
| `watch` | Show animated, timed caption chunks and optionally generate/play speech. |
| `watch --no-color --width 80` | Plain-text output, at most 80 display cells. |
| `watch --no-vfx --no-flash` | Steady captions without delivery animation or arrival flashes. |
| `watch --mute --no-flash` | Disable speech generation/playback and the attention flash. |
| `instructions --format text` | Render shared messaging instructions only; never allocate an identity. |

`instructions --format text` prints just the instruction block. Both plugins use this
helper to require an initial, argument-free `context` call and checked `--id` on sends. Instructions
contain no assigned name or ID; those come from the caller's tool environment.
The old `id`, `prefix`, and session-allocation commands have been removed.

Successful sends print `Estimated total time: N.N seconds (approximate).`
The listener adds the current message's remaining time, all queued speech, and
0.65 seconds of TTS generation overhead per pending message, including the new one.
Speech estimates use `--tts` (or `--msg` when omitted) and Naia's character-based
estimate (about 180 words/minute). Current playback uses measured duration when
available; muted delivery uses caption timing without TTS overhead. This does not
wait for playback or guarantee completion. Restart an older listener to enable
queue-aware estimates; older listeners still accept sends but report no total.

`NO_COLOR` also disables watcher colors. Watch width defaults to 96 cells.
Errors go to stderr and return a nonzero exit status. Offline sends do not launch
a server. Offline watchers show `[! ] comms: offline` and reconnect with backoff
from 250 ms to 5 seconds. Ctrl+C stops the foreground command. The server also
handles SIGTERM for clean shutdown where the OS delivers it.

## Spatial prefixes

`context` checks the current tuios focus and pane geometry on every call:

- Caller is the only agent pane in the currently visible workspace: no required
  prefix, even when another terminal is focused. A hidden workspace or detached
  session still requires attribution. Agents in other workspaces do not count. Idle agents still count;
  unknown peer state conservatively preserves attribution.
- Caller is focused in the currently visible workspace: no required prefix.
- Another pane is focused in the same workspace: `Alice here, to your top-right.`
  Directions are relative to the focused pane, including non-agent panes. Panes
  with overlapping vertical/horizontal spans share that row/column; overlapping
  floating panes use their centers.
- Another workspace is current, or the session is detached: `Alice here, in the work session,
  workspace 2, in the top-left.` Named workspaces use `the main workspace` instead
  of `workspace 2`. Single-axis positions use `at the bottom`, `at the top`,
  `on the left`, or `on the right`; a central pane uses `in the center`.
  Positions use the pane center within thirds of its workspace's pane bounds,
  excluding minimized, unplaced, and scratch panes.
- Unavailable geometry: fall back to the session-only
  introduction rather than guessing. Outside tuios, identity lookup fails.

Copy the exact prefix once at the start of both `--msg` and `--tts`. `send`
uses the same client-side prefix policy as `context`; the listener handles
identity allocation, so an older running listener cannot return a stale prefix.
`send` rechecks focus and geometry, rejecting stale or duplicated introductions before
queueing. On a missing pane identity or identity/prefix mismatch it automatically runs a context lookup and
returns `Fresh context: {"id":...,"name":...,"prefix":...}` on stderr, with a nonzero
exit status. Use that checked result to recompose both message arguments and retry
once; no separate lookup is needed. The message is never rewritten or automatically
resent. If the refresh fails, the original rejection and refresh error are reported;
run `context` successfully before retrying. Restart an existing listener
after upgrading: discovery advertises `spatial_prefix: true`, and spatial sends
to older listeners fail with a restart instruction. Watchers need no protocol
change for spatial prefixes.

Identity allocation inside tuios is pane-scoped, independent of the harness or
conversation. `send` resolves the pane's persisted binding internally and requires
`--id` to match it. This is an assertion, not an identity selector. A mismatch
or missing binding rejects the send and returns a fresh ID, name, and prefix through
an automatic `context` lookup. Missing bindings are allocated by the listener,
not by the sending client. Corrupt state still fails without recovery or allocation.
See ownership and cleanup below.

Run `agent-comms context --clear` in the pane whose mapping you want to release.
It is safe to repeat (`cleared: false`, `id: null` when already unmapped), and
does not change other panes, history, or the shuffled bag. Sends require a fresh
`context` lookup afterward. That lookup allocates an available identity (possibly
the same one) and refreshes the pane's title; clearing alone leaves its title
unchanged. Restart an existing listener once after upgrading to enable clearing.

To make **all agents refresh their identities**, run:

```sh
agent-comms context --clear-all
```

This administrative command clears mappings across all tuios daemons/sessions in
the selected state directory and prints `{"cleared": N}`. It takes the same lock
as allocation and works without a running listener or valid config. It requires
local state write access; there is no sandbox bypass. The roster, shuffle bag,
history, queued announcements, and current pane titles are unchanged. Each pane
must run `agent-comms context` before sending again; a new draw may return the same
identity. Existing listeners read the updated mappings without a restart.
`--clear-all`, `--clear`, and `--refill` are mutually exclusive.

## Configuration and locations

Edit [config.toml](config.toml) in this directory. It is the default on all
platforms, resolved relative to the program rather than your working directory
(including when launched through a symlink). Restart the server after changing it.
An explicitly selected missing config is an error; it is not silently generated.
Watchers receive voice IDs and per-identity speed overrides from the listener's message metadata and read optional
speech mode/model/speed settings from this file; restart watchers after changing those
settings. Missing local config falls back to the Naia speech defaults.

### Restart from tuios

Open tuios's command palette (**Ctrl+A, then `:`**, or Ctrl+A, then Shift+P
with the bundled keymap), and search **Restart agent-comms**:

| Palette action | CLI | Scope |
| --- | --- | --- |
| Restart agent-comms everything | `agent-comms restart all` | Restart installed service, then reload watchers. |
| Restart agent-comms watchers only | `agent-comms restart watchers` | Reload watcher code and speech config; listener unchanged. |
| Restart agent-comms service only | `agent-comms service restart` | Reload listener code/config, including groups; watchers reconnect without reloading. |

The watcher's idle/static badge shows its loaded mode: `[ELE]` (ElevenLabs),
`[PHO]` (Phonon), or `[POC]` (Pocket TTS). Delivery animations temporarily replace
the badge; offline status keeps its warning marker.

Changing only `[speech].mode` needs **watchers only**. Group/identity metadata
changes need the service; use **everything** when changing both. `restart`
without a scope means `restart all`. tuios requires fallback keys for registration:
Ctrl+A then Alt+R (everything), Alt+Shift+W (watchers), or Alt+Shift+S (service).
You can use the palette instead.

Watchers poll an explicit, atomically replaced revision file in their runtime
directory every 250 ms, including while the listener is offline. On change they
cancel presentation, close their Pocket worker, and re-execute with the same
arguments. Service restarts alone do not change this revision and leave a warm
worker intact. All watchers sharing that runtime directory reload; this does not
start missing watchers. No pane bindings or history are cleared. Reloading can
interrupt in-progress audio; Pocket starts cold on its next use. Service restarts
discard the listener's in-memory message queue.

When first upgrading an older watcher, replace it once to load this behavior.
A dock refresh alone may leave an already-running push watcher untouched.

### Identity groups and the random bag

Identities can live under named groups. Select one or more groups at the **top of
the file** (before any table headers):

```toml
active_groups = ["pocket_tts"] # Or ["phonon"], or both.

[identity_groups.phonon.identities.ag_example1]
name = "Example One"
phonon_voice = "Juniper"

[identity_groups.pocket_tts.identities.ag_example2]
name = "Example Two"
pocket_tts_voice = "alba"
```

Only identities in `active_groups` fill the random bag and appear in voice review
(including `--all`). The bundled groups are `phonon` (the original 25 identities,
including preserved ElevenLabs IDs) and `pocket_tts` (21 identities, one for each
English preset in Pocket TTS 4.0.1). Six other-language presets are not included
in the English group. Display names follow the Pocket presets.

Group selection and `[speech].mode` are independent: set both when changing
provider. Blank voices for the selected provider remain text-only. Multiple
groups may be active, but each identity needs a voice for the selected provider
to speak. IDs must be globally unique, even across inactive groups.

Omitting `active_groups` includes all identities, preserving legacy flat
`[identities.ID]` configs; flat and grouped identities can coexist. Explicit group
selection excludes ungrouped identities. Empty/unknown/duplicate active group
lists fail validation. Changing membership rebuilds the bag on its next draw.

**Existing pane bindings remain stable**, even if their group becomes inactive.
To move a pane to the newly selected group, run `agent-comms context --clear` in
that pane, then run `agent-comms context`. Never clear other panes' bindings.
Restart agent-comms after editing groups or speech settings (see above).
Review decisions update the original nested TOML table without
flattening the file or modifying other groups.

Run `agent-comms voice-review` in an interactive terminal with a supported audio
player installed. It uses `[speech].mode`: Pocket TTS needs `pocket-tts`, Phonon needs `ptts`; ElevenLabs needs
`ELEVENLABS_API_KEY` and consumes API credits. Use `agent-comms voice-review --all`
to include approved voices in the active groups. Samples play without robot effects or normalization,
but honor the configured speed. The prompt accepts:

- `y` / `yes`: approve this provider's voice.
- `n` / `no`: mark this provider's voice not approved.
- `s` / `skip`: stop the sample and move on **without changing any approval or
  rejection history**, including when reviewing an already-approved voice.
- `r` / `replay`: restart the same audio sample from the beginning without saving
  a decision or generating new speech (no additional ElevenLabs request).
- `q` / `quit` (or EOF): stop the review without changing the current voice.

Answer during or after playback; replay stops any current player before restarting.
Your approval, skip, or quit stops the current audio immediately.
Approvals are independent: ElevenLabs uses `voice_approved`, Phonon uses
`phonon_voice_approved`, and Pocket TTS uses `pocket_tts_voice_approved`.
Missing approval means false. The default run reviews only
non-approved voices for the selected provider; rejected and never-approved skipped
voices are offered again. Each yes/no is saved immediately, so quitting or Ctrl+C
keeps completed decisions. Blank voices are skipped without changes. Approval is
for this workflow only, not identity allocation or ordinary message playback.

In ElevenLabs mode, every no answer also adds the voice ID to `[rejected_elevenlabs_voices]`'s
`ids` array, without duplicates. Agents finding replacement voices should read
this list and never reuse its IDs. This is persistent rejection history: later
approval does not automatically remove an ID; edit the list manually if you
want to allow it again. Missing `voice_approved` does not add an ID to this list.
Phonon and Pocket TTS decisions do not modify this ElevenLabs rejection list.

```toml
[rejected_elevenlabs_voices]
ids = ["previously-rejected-voice-id"]
```

```toml
display_seconds = 5

[speech]
mode = "phonon" # "phonon", "pocket_tts", or "elevenlabs" (legacy default).
model = "eleven_turbo_v2_5"
elevenlabs_speed = 1.0
phonon_speed = 1.0
pocket_tts_speed = 1.0
normalize = true
robot_mode = true

[speech.phonon]
model = "hf://gradium/phonon-31m-en"
executable = "~/.local/bin/ptts" # Or "ptts" if it is on the watcher's PATH.

[identities.naia]
name = "Naia"
eleven_labs_voice_id = ""
phonon_voice = "Juniper"
voice_approved = false
phonon_voice_approved = false
elevenlabs_speed = 1.1 # Optional per-identity override.
phonon_speed = 1.0
```

### Local Pocket TTS

Install outside the repo, in a separate uv tool environment. Python 3.11 avoids
dependency compatibility surprises and does not change agent-comms' interpreter:

```sh
uv tool install --python 3.11 pocket-tts==4.0.1
# Warm the model/preset cache; output goes outside the repo.
pocket-tts generate --language english --voice alba --text "Hello from Pocket TTS." --output-path "$TMPDIR/pocket-test.wav"
```

Select the Pocket group and backend:

```toml
active_groups = ["pocket_tts"] # Top level, before table headers.

[speech]
mode = "pocket_tts"
pocket_tts_speed = 1.0

[speech.pocket_tts]
executable = "~/.local/bin/pocket-tts"
language = "english"
```

Each identity's `pocket_tts_voice` accepts a preset name, a local reference WAV,
or an exported `.safetensors` voice. Relative file paths resolve from the directory
containing `config.toml`, not the listener's working directory. Absolute paths
and `~/` paths also work. For example, store a clone in `agent-comms/voices/`:

```toml
[identity_groups.pocket_tts.identities.ag_example2]
name = "My voice"
pocket_tts_voice = "voices/my-voice.safetensors" # Or "voices/my-voice.wav".
pocket_tts_voice_approved = false # Audition with agent-comms voice-review first.
```

From the `agent-comms` directory, export a permitted reference recording once:

```sh
pocket-tts export-voice --language english /path/to/reference.wav voices/my-voice.safetensors
```

Export uses up to the first 30 seconds. Use the same model/language for export
and synthesis; re-export after changing models. Local cloning can
require access to the upstream gated cloning weights and acceptance of their
terms. Public preset voices worked without a token in this setup. No terms are
accepted automatically. Use recordings you have permission to clone; preset
recordings and model weights can have separate licenses.

Runtime dependencies (including PyTorch) are in `uv tool dir`, outside this repo;
model and preset assets are cached under `~/.cache/huggingface/hub` and/or
`~/.cache/pocket_tts`. This environment is substantially larger than Phonon's
Rust-based runtime. First downloads require network access; cached generation
needs no paid API key. Missing tools or synthesis failures yield captions, never
a cloud fallback; voice review reports the failure and stops.

Each watcher owns a private resident Pocket worker in the installed tool's Python
environment. It starts only after that watcher wins a speech lease for an actual
announcement. The model loads once, and its in-memory voice-state cache starts
empty: a voice is loaded only on first use, then reused when selected again.
There is no roster preloading. Muted, text-only, and unclaimed messages load no
voice states. Previously downloaded preset files remain in the user disk cache;
this is separate from the lazy in-memory cache.

The worker stays warm between announcements and watcher reconnects. It uses
serialized JSON requests through private pipes (no network listener), bounded
PCM WAV validation, and complete-WAV playback, not streaming playback. A synthesis
timeout, worker failure, or cancellation during generation stops the worker before
removing partial audio. Its model/cache are recreated lazily on the next request.
Cancelling playback or audio effects after generation leaves the idle model warm.
Exiting the watcher closes its worker; other watchers do not share its memory.

The normal uv install's `pocket-tts` executable locates its environment's Python
automatically. For unusual installations, set `python_executable` under
`[speech.pocket_tts]` to an interpreter with Pocket TTS installed. The agent-comms
listener/core does not import PyTorch.

`voice-review` still uses the one-shot CLI per new sample; replay reuses that
sample without regeneration. Its auditions do not populate the watcher's cache.
Like Phonon, non-default speed uses pitch-preserving FFmpeg `atempo`; announcement
effects and volume normalization are unchanged. Review bypasses effects and
normalization, and supports yes/no/skip/replay/quit for Pocket too.

After changing provider/groups, use **Restart agent-comms everything** in tuios's palette,
or run:

```sh
agent-comms restart all
```

Already-bound panes retain their identity; clear and re-resolve only the panes you
want to move to the Pocket group. Restart existing reviews to pick up the changes.

For the one-time upgrade from an older watcher, stop only its verified PID, then
refresh the dock component. Do not terminate the terminal, tuios daemon, or
unrelated watcher processes. Updated watchers reload on the explicit watcher signal.

Sources: [upstream runtime](https://github.com/kyutai-labs/pocket-tts),
[model terms](https://huggingface.co/kyutai/pocket-tts),
[reference voice licenses](https://huggingface.co/kyutai/tts-voices).

### Local Phonon 31m

Install the optional runtime **outside this repository**, in uv's isolated user
tool environment (the agent-comms core remains standard-library-only):

```sh
uv tool install ptts==0.4.2
ptts --list-voices
# Warm the user cache before enabling speech; write the sample outside the repo.
ptts --model hf://gradium/phonon-31m-en --lang en --quant q8_0 --device cpu --voice Juniper -o "$TMPDIR/phonon-test.wav" "Hello from Phonon."
```

On Windows use an appropriate temporary output path and set `executable` to
the installed `ptts.exe` path if needed. `uv tool dir` shows the isolated tools;
model weights (~64 MB) and requested voices go into the user Hugging Face cache
(normally `~/.cache/huggingface/hub`), not this repo. The first use needs network
access to download these public assets; cached synthesis is local and needs no
ElevenLabs or Gradium API key. Phonon 31m weights are Apache 2.0.

Set `[speech] mode = "phonon"` and each identity's `phonon_voice` to a catalog
name. Blank/missing names keep that identity text-only in Phonon mode. The
bundled `phonon` group has 25 identities with distinct conversational English voices;
the existing ElevenLabs approval flags do not represent Phonon auditions.
Keep the `eleven_labs_voice_id` fields: switching `mode` back to `"elevenlabs"`
restores them. Failures fall back to captions, **never to the other provider**.
`voice-review` auditions the provider selected by `mode`; `--all` includes approved voices.

Use `agent-comms restart watchers` after changing speech mode/settings, or
`agent-comms restart all` when also changing identity groups or listener code.
Older watchers require one manual restart when first upgrading to this behavior.
Invalid configs fail closed rather
than silently selecting ElevenLabs.

`agent-comms instructions` renders the selected mode and only that provider's
speech guidance before the harness plugins inject it. No provider inference by
the agent is needed. Existing sessions may retain previously injected guidance;
fresh sessions receive the current mode (or reload the rendered instructions).

Phonon currently uses a cancellable `ptts` subprocess and the existing complete-WAV
playback path, not streaming playback or a permanently resident model. Playback
starts after synthesis and optional effects, so the model's advertised streaming
latency is not the app's latency. SSML break tags from older agents are removed;
new agents are instructed to send plain text. Non-default speed uses FFmpeg's
pitch-preserving `atempo` filter; install FFmpeg to use speed overrides. Set
`robot_mode = false` to audition the unmodified voice (existing effects are retained
by default in this config). The normalizer remains independently configurable.

`elevenlabs_speed`, `phonon_speed`, and `pocket_tts_speed` configure independent speed multipliers,
globally in `[speech]` or within each identity table. All apply to `watch` and
`voice-review` and must be numbers from 0.7 to 1.2. Resolution order:

1. Identity's selected-provider speed (`phonon_speed`, `pocket_tts_speed`, or `elevenlabs_speed`).
2. Identity's legacy `speed`, if present.
3. `[speech]` selected-provider speed.
4. `[speech].speed` (legacy fallback), then 1.0.

ElevenLabs receives the multiplier in `voice_settings.speed`; Phonon and Pocket TTS use local
FFmpeg `atempo` processing without changing pitch. A provider's setting never
affects the other provider. The bundled config preserves the previous rates:
global 1.0 for all providers, and Tarquin's identity override 1.2 for ElevenLabs and Phonon.
Restart agent-comms after changing speed settings; a fresh
`voice-review` process reads the current config without a restart.

`watch` normalizes speech volume across voices by default using optional `ffmpeg`
preprocessing (−16 LUFS target, −1 dBTP peak ceiling). Processing finishes before timed captions begin;
playback and captions use the processed WAV's measured duration. Missing or failing
FFmpeg falls back to the original audio with a warning. Set `[speech] normalize = false`
to disable it. Restart agent-comms to load this setting; `voice-review` is unchanged.

`[speech] robot_mode = true` enables the exact robot filter graph/settings from
Neovim Naia: layered delays/chorus/tremolo, weighted mixing, EQ, compression,
5 dB gain, and a 0.92 limiter. The bundled config enables it, matching Naia;
set `robot_mode = false` for natural voices (also the default when omitted).
Robot effects run before normalization, and either setting works independently.
If effects fail or FFmpeg is missing, playback keeps the input audio and warns.
Caption timing includes the processed audio's effect tail. Restart `watch` after
changing the config; `voice-review` always auditions the original voice.

The starter roster is Naia, Nova, Atlas, and Echo. Fill each
`eleven_labs_voice_id` with a voice available to your ElevenLabs account to enable
that identity's speech. Blank voice IDs deliberately remain text-only.
Identities have names and voices, not personalities. Obsolete `personality`
fields in older configs are ignored and never returned or injected.

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

## Live voice controls

```sh
agent-comms audio toggle
agent-comms audio mute --seconds 60
agent-comms audio mute           # until explicitly unmuted
agent-comms audio unmute
agent-comms audio status         # JSON; no identity/config needed
```

These commands control the shared listener, so every connected watcher honors
the same mute. Captions and history continue; current playback is stopped and
speech generation is skipped while muted. An already-started HTTP synthesis
request cannot be interrupted, but its late audio is cleaned up and never played.
Unmuting does not replay skipped speech. `watch --mute` remains a permanent local
override, unaffected by shared unmute commands.

`--seconds` accepts a positive duration up to 86400 (one day), for `mute` only.
Repeating timed mute resets the countdown from now; toggle/unmute cancels it.
The dock shows `[Muted 43s]` or `[Muted]` ahead of the normal captions. Mute state
is in memory: restarting the listener resets it to unmuted.

The checked-in tuios config binds **Ctrl+A, Alt+M** to toggle and
**Ctrl+A, Alt+Q** to mute for 15 seconds. Edit `--seconds 15` in its
`[[keybindings.command]]` entry to change that shortcut's duration. Commands run
in a temporary popup and never type into the focused agent or shell.

After installing this update, restart the existing listener and watchers once
to load the code. Later mute changes need no restarts. For the dock watcher,
run `tuios refresh-dock agent-comms` in each attached session.

## Pane name prefixes

`agent-comms context` also names its own pane `[NAME] Current terminal title`.
Each lookup refreshes the suffix from tuios's live terminal title, not the
previous custom display name. Missing, wrong, or stacked agent prefixes are
normalized to the current identity. Only configured agent names and the binding's
previous exact prefix are recognized; unrelated bracketed labels in the terminal
title remain intact. Custom pane names are replaced by this normalized live title
on lookup; there is no full-name ownership tracking.

Names are custom tuios pane names, not terminal escape titles: they refresh only
on `context` and accepted pane-scoped `send` calls, not continuously or through
title-change subscriptions. Send refreshes run best-effort in the listener after
acceptance, independently of message delivery; naming failures never reject the
message. The listener verifies the pane's existing identity and live context,
coalesces concurrent refreshes per pane, and never allocates an identity on send.
Clients still perform no state writes. Legacy clients without pane addressing
can send normally but do not request a title refresh. Clear the
custom name with `tuios set-window -w PANE --name ''` to return to the shell's
title (the next context call prefixes it again). Naming errors warn on stderr
without breaking context's three-field JSON or retrying denied requests.

## Delivery effects

Colored watchers animate at 10 frames per second while a message is active:
an orbiting dot while speech is preparing, then a decorative pulsing waveform
with a cyan/white glow sweeping across the identity. This is an activity effect,
not an audio-level meter. Captions stay in the dock's normal ink, with unchanged
page timing and width. Idle and offline output remain static; effects use only
single-line text and ANSI SGR, not cursor movement or terminal-screen commands.

Effects are enabled by default (`--vfx`); `--no-vfx` disables them independently
of the existing arrival flash. Use `--no-vfx --no-flash` for no motion.
`--no-color` or `NO_COLOR` disables both effects and flashes automatically.
Restart existing watchers to load updated code; the tuios dock command explicitly
includes `--vfx` so the updated configuration restarts its push component.

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
agent-comms context
# Inspect the returned JSON. Use its exact prefix in the next tool call:
agent-comms send --id RETURNED_ID --msg "RETURNED_PREFIX Build finished. Tests passed." \
  --tts "RETURNED_PREFIX Build finished. <break time=\"0.3s\" /> Tests passed."
```

The API returns 24 kHz mono PCM; the watcher wraps it in a temporary private WAV,
measures its exact sample duration, and deletes it after playback/cancellation.
Audio lives under the OS temporary directory (`tempfile.gettempdir()`), in
`agent-comms-<user-hash>/audio/speech-<random>/`, not persistent application state.
Each workspace holds an OS-backed lock through synthesis, filtering, and playback,
including a late synthesis result after cancellation. On the next speech preparation,
workspaces older than 24 hours are removed only if their lock is no longer held.
This recovers orphans after crashes/hard kills without deleting another active
watcher's audio. Cleanup is opportunistic, not a background retention guarantee.
Existing WAVs in the old state `audio/` directory are not migrated or removed.
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

## Checked identity on sends

Agents must initially call `agent-comms context` in a **separate tool call**,
read the JSON result, then compose both arguments using its returned name and
exact prefix and send with `--id RETURNED_ID`. Reuse these details for later sends
in the same pane, refreshing on an identity/prefix mismatch. Never inherit an ID
from another pane or conversation or hide the initial lookup in a shell variable.
Never pass `--harness`, `--session-id`, or an identity to `context`: those
flags are rejected. Ownership is derived only from the caller's pane environment.
Outside tuios, lookup fails clearly rather than accepting an agent-selected owner.

```json
{"id":"ag_example","name":"Veda","prefix":"Veda here, to your top-right."}
```

Output contains exactly those three fields; `prefix: ""` means no fixed
introduction is required. Context stays internal. Lookup uses read-only
`tuios session-info` and `tuios list-windows`, with two-second timeouts.
Incomplete environment, an unknown pane, or failed/forbidden context queries
produce an error, not a guessed empty prefix. No pane is focused or manipulated.

Identity allocation is performed by the running listener through its existing
authenticated localhost API. The CLI checks its own live pane context first and
forwards only tuios pane/session/host/socket addressing, never arbitrary environment
variables or credentials. The listener verifies that pane again before allocating
or updating its title. Neither `context` nor `send` writes the client's state files:
`send` reads the atomically replaced binding without creating directories, changing
permissions, or opening lock files. This supports Codex's workspace-write sandbox
without adding the application state directory to its writable roots. Localhost
access must still be permitted, and the caller must retain its `TUIOS_*` environment.

After upgrading, restart the agent-comms listener once. Older listeners produce
an explicit restart message; there is no fallback to client-side state writes.
On macOS, tuios discovery checks the normal Homebrew executable directories when
launchd's minimal PATH cannot locate it. A found executable is never retried via
another path after a permission or query failure. Errors distinguish client pane
checks from listener identity lookup and report missing executables separately
from access denials.
`context --refill`, `context --clear-all`, and `voice-review` remain explicit local maintenance commands
and still require write access to their state/configuration.

**Nothing is automatically inserted.** Put nonempty `prefix` once at the start
of both `--msg` and `--tts`, before markup. Do not add a second introduction.
With an empty prefix, introduce yourself using the returned name on your first
message (or when identity changes); otherwise go straight to the update.

`send` checks the required `--id` against the pane identity and rechecks spatial context before networking. Missing,
stale, wrong, or duplicated introductions are rejected without queueing, history,
or speech. On mismatch, call `context` again and recompose once. Never silently retry.
The listener checks the submitted snapshot too; this is a workflow check, not
authentication against clients that can falsify their own context.

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
command = 'exec "$HOME/.local/bin/agent-comms" watch --width 80 --flash --vfx'
refresh = "push"
max-width = 80
```

The existing dock remains at the bottom. Each stdout line replaces the component
text; SGR colors survive, but cursor movement and clearing do not. This is not an
embedded terminal or a full-width/multi-row rail. Other components share the bar,
and tuios may truncate it further. This tuios version caps custom components at
80 cells, so the integration requests 80 even though standalone `watch` defaults
to 96. The quiet idle display is `[o_o] comms: idle`.

### Persistent macOS service

```sh
agent-comms service install      # Write a per-user LaunchAgent; no sudo
agent-comms service start        # Load it, then wait for authenticated readiness
agent-comms service status       # JSON: installed, loaded, PID, readiness, paths
agent-comms service logs         # Last 100 lines of lifecycle/startup logs
agent-comms service logs --follow
agent-comms service restart      # Discards the in-memory queue
agent-comms service stop         # Unload, so KeepAlive cannot undo the stop
agent-comms service uninstall    # Stop and remove only the LaunchAgent
```

launchd owns the server, starts it at login, and restarts it on exit with a
10-second throttle. Neither closing an agent nor restarting tuios stops it.
`start` is idempotent: it does not kill/restart an already-running service.
Mutating service commands serialize across callers. Install is explicit;
starting a missing service reports how to install it. Commands never kill an
unmanaged foreground listener: stop that listener before migrating to the service.

The definition is `~/Library/LaunchAgents/com.agent-comms.server.plist`. It pins
the current Python interpreter, source script, and resolved config/state/runtime
paths, using an argument array rather than shell quoting. Keep the checkout and
Python installation in place. After moving/upgrading them, stop, install, and
start again. No API keys or pane environment are copied into the job; speech
still runs in the credential-bearing dock watcher, not the global listener.
Commands using different config/state/runtime overrides refuse to manage an
existing job for other paths. Linux still uses foreground `serve`; Windows has
the experimental Task Scheduler backend described below.

Lifecycle logs live in `STATE/logs/server.log` (2 MB, three rotated backups).
They record startup, readiness, shutdown signals, and failures, not messages or
transport tokens. launchd stdout/stderr files in the same directory capture
early startup failures; these are not rotated automatically. Uninstall preserves
logs, history, and identity bindings.

This configuration also requests an idempotent start whenever a tuios client
attaches (including opening tuios):

```toml
[hooks]
after-attach = ['"$HOME/.local/bin/agent-comms" service start --if-installed']
```

The hook only asks the OS supervisor to start an **already installed** service.
It never installs anything or hosts the listener as a child process. Multiple
clients are safe. A deliberate `service stop` lasts until the next login or
tuios attach; uninstall to disable both automatic starts. New clients read the
hook from configuration; no tuios daemon restart is required for this client-side
hook. Use service status/logs to diagnose startup failures. Existing dock watchers
reconnect automatically.

### Experimental Windows service

**Implemented but not execution-tested on Windows.** Run these from PowerShell
in the `agent-comms` directory, with Python 3.11+ installed:

```powershell
.\agent-comms.cmd service install
.\agent-comms.cmd service start
.\agent-comms.cmd service status
.\agent-comms.cmd service logs --follow
.\agent-comms.cmd service restart
.\agent-comms.cmd service stop
.\agent-comms.cmd service uninstall
# Equivalent without the .cmd launcher:
py -3 .\agent_comms.py service status
```

This registers `agent-comms-<your Windows SID>` in Task Scheduler's root folder.
It runs **only while you are signed in**, as your current user with least
privilege, using your interactive token. No password is requested/stored, no
elevation is attempted, and no machine-wide Windows service is installed.
Organization policy may disallow registration; errors are reported rather than
bypassing policy. Windows PowerShell 5.1 and the Task Scheduler service are
required. WSL uses Linux, not this backend.

The task starts at sign-in, ignores duplicate starts, has no execution-time
limit, and runs on battery power. On nonzero exit, Task Scheduler retries after
**one minute**, up to **255 retries**; this is bounded recovery, not launchd's
indefinite keep-alive. Once retries are exhausted, use `service restart` and
inspect the logs/Task Scheduler history. The normal status command distinguishes
task state from authenticated listener readiness, checking a per-installation
ID as well as the listener PID.

The task pins the Python interpreter (preferring its adjacent `pythonw.exe`),
checkout, and explicit config/state/runtime paths. The action runs the listener
**in its own PID**, not as an untracked child. Paths with spaces/Unicode are XML-
escaped and Windows-argv-quoted independently. Task Scheduler operations use its
COM API through fixed PowerShell source with JSON input, not localized `schtasks`
tables or dynamically interpolated commands. Caller pane variables, agent-comms
overrides, and speech credentials are removed from the runner environment.

`service stop` **disables the task before terminating it**, preventing automatic
recovery from undoing your stop. Unlike macOS, sign-in startup stays disabled
until `service start` (or the tuios attach hook) re-enables it. Termination may be
forced: do not expect Python signal handlers, discovery-file cleanup, or a
graceful shutdown log entry. The next listener replaces stale discovery under
the existing singleton lock. Stops/restarts discard the in-memory queue.
No process-name or machine-wide process-tree kills are used.

Logs default to `%LOCALAPPDATA%/agent-comms/state/logs`: rotating `server.log`
plus `windows.stderr.log` for early errors (not automatically rotated). Log
following is implemented in Python; it does not require Unix `tail`, and releases
file handles between reads so Windows can rotate logs. Uninstall preserves logs,
history, and identities. Stop/install/start again after moving Python or the
checkout. As on macOS, installation never silently replaces an unmanaged listener
or a task with unrelated ownership/path metadata.

For tuios, the same `service start --if-installed` command is available. **Do not
copy the macOS `$HOME/.local/bin` hook onto Windows.** A Windows-oriented snippet
(adjust the checkout path) is:

```toml
[hooks]
after-attach = ['py -3 "C:/path/to/agent-comms/agent_comms.py" service start --if-installed']
```

This does not remove tuios's current dependency on `sh` for shell commands:
native Windows still needs an available compatible shell or the upstream
platform-aware command-runner work in [WINDOWS-SUPPORT.md](WINDOWS-SUPPORT.md).
The scheduled task itself does not need `sh` or tuios, so sign-in startup and
service commands can be used independently of that integration.

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
lookup instructions. Concurrent initialization is coalesced. A bounded
128-session cache stores **instructions only**, never a name, ID, or prefix.
The agent's pre-send `context` call runs in the actual pane's tool environment, rather
than attributing shared-server plugin processes to the wrong pane. Reload after
changing the communication template. Metadata is read fresh by `context`.

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

### Ownership and cleanup

Inside tuios, `state/pane-identities.json` maps **daemon + pane UUID** to identity
under one OS lock. The daemon key hashes the stable host/socket path (or named
server fallback), never a PID or secret token. Workspace/session moves, harness
switches, forks in the same pane, and process restarts reuse the binding.
Separate panes receive distinct IDs; roster exhaustion is explicit, not silent
identity reuse. Retained bindings reserve their names until cleanup.

On `context`, at most hourly per daemon, a complete successful read-only
`tuios list-windows --all --json` snapshot checks every session, including
detached panes. Confirmed missing panes get a seven-day grace period. A later
successful snapshot after that period removes the binding; a returning pane
clears its missing marker. Failed, forbidden, partial, or inconsistent snapshots
never remove anything. Only records for the currently queried daemon are pruned;
records for an unreachable/decommissioned daemon are retained for manual review.
One shared lock file avoids accumulating a lock file for every retired pane.

There is no harness/session fallback. `context` outside tuios errors instead of
drawing a new identity or accepting caller-selected identity arguments.
`context --refill` reshuffles the allocation bag without changing existing bindings.

The first pane lookup creates a new pane binding; legacy session mappings are
no longer read or written, and are not silently deleted. That can change the
speaker once on upgrade.
Metadata comes from current config. Removed identities are reassigned on lookup;
corrupt mappings fail explicitly. Allocation and binding use atomic writes; a
crash between them can consume an unused bag entry but cannot expose partial JSON.
Child-agent-specific Codex hooks are not installed; the adapter targets SessionStart.

[communication-instructions.md](communication-instructions.md) is the single
template used by both adapters. It defines the communication rhythm:
open longer tasks, send useful progress, and **close every turn immediately
before the final text response**, even for short answers. It preserves the
native-voice exception and Claude Code readability guidance, requests editor
cards before the closing message, and covers safe quoting, secrets, existing
permissions, and continuing without retry spam when the listener is offline.

Configure the human user's name separately from agent identities in `config.toml`:

```toml
[user]
name = "Oli"       # Written responses and --msg.
tts_name = "Ollie" # Pronunciation spelling for --tts; defaults to name.
```

Both harness plugins inject these names through `agent-comms instructions`.
Names are optional; without `[user]`, instructions use generic user references.
Neovim's static instructions do not define a user name. This guides agent-authored
text and speech; it does not automatically rewrite messages or agent prefixes.
OpenCode caches rendered instructions per session, so use a fresh session after
changing these values (or reload the plugin); Codex picks them up at its next
SessionStart hook, including resume/compact.

Both adapters require normal, clear communication, with no identity-specific
personality. Speech examples and the injected current model name reflect the
Naia-default Turbo model. Use `/reload` in OpenCode; refresh/reinstall Codex's
cached plugin through its normal plugin workflow, then resume the conversation
so revised instructions replace older hard-coded identity/prefix guidance.

Neovim display/navigation guidance now lives in
`nvim/agent/skills/naia/SKILL.md`, separate from communications instructions;
the attachment shim injects only a short notice and makes that skill discoverable.
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
Restarting the listener does not fix that restriction. Both normal identity
lookup and sending require localhost access; the SessionStart hook only supplies
instructions and does not allocate identities.

Use Codex's normal approval flow for any additional access. Do not automatically
enable general networking, bypass the sandbox, auto-approve commands, or silently
switch transports. A separately authorized narrow messaging tool is another
possible integration, but is not installed by this version.
