# agent-comms

Generated agent names with gender-matched voices, and a per-user FIFO message
listener with live colored tuios dock output and an optional conversation view.
Python **3.11+**, standard library only. See [SPEC.md](SPEC.md) for the design.

## Run

macOS/Linux, from this directory:

```sh
chmod +x agent-comms
./agent-comms serve
```

For a persistent macOS listener, prefer the per-user launchd service below.
`serve` is a foreground/debugging command; do not use an agent's background
shell tool to host a global listener.

In another terminal shell or tuios pane:

```sh
# Inspect the JSON first, then use its name, epoch, and exact prefix in a separate call.
./agent-comms context
# Replace the placeholders below (omit an empty prefix).
./agent-comms send --name RETURNED_NAME --epoch RETURNED_EPOCH --msg "RETURNED_PREFIX Hello from the agent dock!" --tts "RETURNED_PREFIX Hello from the agent dock!"
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
.\agent-comms.cmd send --name $lookup.name --epoch $lookup.epoch --msg $message --tts $message
```

## Commands

| Command | Behavior |
| --- | --- |
| `context` | Resolve the pane or shell's assignment; print `id` (alias of `name`), `name`, `epoch`, and `prefix` as JSON. Reuse until a send returns fresh context on mismatch. |
| `context --clear` | Release only the caller pane or shell's binding; print `cleared` and the previous `name` (plus `id`) as JSON. The name stays spent; the next `context` allocates a fresh one. |
| `context --clear-all` | Release every pane and shell binding in the selected state directory; print the number cleared as JSON. Names stay spent. Works outside tuios and with the listener offline. |
| `context --refill` | Retired: fails with guidance. Names refill automatically; use `bags reset --confirm` for an explicit restart. |
| `bags status` | Print the epoch, name generation, remaining name/voice bag sizes, and bound names as JSON. |
| `bags reset --confirm` | Start a new epoch at generation 0 and clear every binding; also migrates legacy state. See [Explicit reset](#explicit-reset-bags-reset---confirm). |
| `voice-review` | Audition non-approved voice profiles using the configured TTS mode; approve, reject, skip unchanged, replay, or quit. |
| `voice-review --all` | Audition every active voice profile, including already-approved ones. |
| `serve` | Own the singleton listener, timed FIFO queue, and terminal display. |
| `serve --quiet` | Same listener without terminal display. |
| `service install/start/stop/restart/status/logs/uninstall` | Manage the per-user macOS LaunchAgent or experimental Windows scheduled task; see below. |
| `send --name NAME --epoch EPOCH --msg TEXT --tts SPEECH` | Verify the expected name and epoch against the pane or shell binding, save and queue text; print approximate total time and the message ID. `--id` is a deprecated alias for `--name`. Optional display metadata: see [Conversation view](#conversation-view). |
| `history` | Show readable, wrapped history with timestamps and sender/session headings, even offline. Colors appear only in a terminal (respecting `NO_COLOR` / `--no-color`). |
| `history --raw` | Print unstyled, one-line timestamp/name/message records. |
| `history --json` | Print full machine-readable records, including speech text. Mutually exclusive with `--raw`. |
| `history --limit 5 --name Alice --json` | Filter/limit retained history and optionally include complete metadata and speech text. Add `--epoch EPOCH` to tell apart names repeated after a reset. |
| `history --session work` | Filter by the sender's tuios session name. |
| `watch --view log` | Follow accepted messages, queue state, and live playback in a silent animated log. |
| `watch` | Show animated, timed caption chunks and optionally generate/play speech. |
| `watch --no-color --width 80` | Plain-text output, at most 80 display cells. |
| `watch --no-vfx --no-flash` | Steady captions without delivery animation or arrival flashes. |
| `watch --mute --no-flash` | Disable speech generation/playback and the attention flash. |
| `watch --view conversation [--no-animation] [--no-color]` | Silent, pane-sized view of who is speaking to whom. |
| `instructions --format text` | Render shared messaging instructions only; never allocate a name. |

`instructions --format text` prints just the instruction block. Both plugins use this
helper to require an initial, argument-free `context` call and checked
`--name`/`--epoch` on sends. Instructions contain no assigned name or epoch; those
come from the caller's tool environment.
The old `id`, `prefix`, and session-allocation commands have been removed.

Successful sends print `Estimated total time: N.N seconds (approximate).`
followed by `Message ID: <32 hex digits>`. Non-fatal warnings (such as an unknown
display recipient) appear on stderr as `agent-comms: warning: ...`.
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
  When only one workspace is in use, omit its name/number:
  `Alice here, in the work session, in the top-left.` Count workspaces containing
  non-scratch panes plus the current workspace; unused empty slots do not count.
  Positions use the pane center within thirds of its workspace's pane bounds,
  excluding minimized, unplaced, and scratch panes.
- Unavailable geometry: fall back to the session-only
  introduction rather than guessing. Outside tuios, identity lookup fails.

Copy the exact prefix once at the start of both `--msg` and `--tts`. `send`
uses the same client-side prefix policy as `context`; the listener handles
name allocation, so an older running listener cannot return a stale prefix.
`send` rechecks focus and geometry, rejecting stale or duplicated introductions before
queueing. On a missing binding or a name/epoch/prefix mismatch it automatically runs a context lookup and
returns `Fresh context: {"id":...,"name":...,"epoch":...,"prefix":...}` on stderr, with a nonzero
exit status. Use that checked result to recompose both message arguments and retry
once with `--name NAME --epoch EPOCH`; no separate lookup is needed. The message is never rewritten or automatically
resent. If the refresh fails, the original rejection and refresh error are reported;
run `context` successfully before retrying. Restart an existing listener
after upgrading: discovery advertises `spatial_prefix: true`, and spatial sends
to older listeners fail with a restart instruction. Watchers need no protocol
change for spatial prefixes.

Name allocation inside tuios is pane-scoped, independent of the harness or
conversation. `send` resolves the pane's persisted binding internally and requires
`--name` and `--epoch` to match it. These are assertions, not selectors. A mismatch
or missing binding rejects the send and returns a fresh name, epoch, and prefix through
an automatic `context` lookup. Missing bindings are allocated by the listener,
not by the sending client. Corrupt or legacy state still fails without recovery or allocation.
See ownership and cleanup below.

Run `agent-comms context --clear` in the pane whose binding you want to release.
It is safe to repeat (`cleared: false`, `name: null` when already unbound), and
does not change other panes, history, or the bags. The released name is **not**
returned to the name bag. Sends require a fresh `context` lookup afterward. That
lookup allocates a new name (with a newly drawn voice) and refreshes the pane's
title; clearing alone leaves its title unchanged.

To make **all agents pick up fresh names**, run:

```sh
agent-comms context --clear-all
```

This administrative command clears bindings across all tuios daemons/sessions and
shells in the selected state directory and prints `{"cleared": N}`. It takes the
allocation lock and works without a running listener or valid config. It requires
local state write access; there is no sandbox bypass. The name and voice bags,
generation, epoch, history, queued announcements, and current pane titles are
unchanged, so issued names stay spent and new names continue from the bag. Each
agent must run `agent-comms context` before sending again. Existing listeners read
the updated bindings without a restart. `--clear-all` and `--clear` are mutually
exclusive. To start over at generation 0 instead, use
[`bags reset --confirm`](#explicit-reset-bags-reset---confirm).

## Configuration and locations

Edit [config.toml](config.toml) in this directory. It is the default on all
platforms, resolved relative to the program rather than your working directory
(including when launched through a symlink). Restart the server after changing it.
The [names.toml](names.toml) catalog sits beside it (see `names_file`).
An explicitly selected missing config is an error; it is not silently generated.
Watchers receive voice IDs and per-profile speed overrides from the listener's
message metadata (taken from each agent's voice snapshot) and read optional
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

Changing only `[speech].mode` needs **watchers only**. Voice-profile, group, or
name-catalog changes need the service; use **everything** when changing both. `restart`
without a scope means `restart all`. tuios requires fallback keys for registration:
Ctrl+A then Alt+R (everything), Alt+Shift+W (watchers), or Alt+Shift+S (service).
You can use the palette instead.

Watchers poll an explicit, atomically replaced revision file in their runtime
directory every 250 ms, including while the listener is offline. On change they
cancel presentation, close their Pocket worker, and re-execute with the same
arguments (conversation-view watchers too). Service restarts alone do not change this revision and leave a warm
worker intact. All watchers sharing that runtime directory reload; this does not
start missing watchers. No name bindings or history are cleared. Reloading can
interrupt in-progress audio; Pocket starts cold on its next use. Service restarts
discard the listener's in-memory message queue.

When first upgrading an older watcher, replace it once to load this behavior.
A dock refresh alone may leave an already-running push watcher untouched.

### Generated names

Agents no longer have static identities. Each new agent (pane or shell) gets a
**generated name** from [names.toml](names.toml), referenced at the top of
`config.toml` by a path relative to the config file:

```toml
names_file = "names.toml"
```

```toml
# names.toml — illustrative subset
male = ["Adam", "Ben", "Leo"]
female = ["Alice", "Clara", "Maya"]
```

- Only `male` and `female` arrays are allowed; array membership sets the name's
  gender. Names carry no IDs, voices, or other metadata.
- Each name is 1–32 ASCII letters starting with an uppercase letter: no digits,
  spaces, punctuation, or suffixes. Names must be unique **case-insensitively
  across both arrays**. `user` (any case) is reserved for the conversation view.
  Invalid or duplicate entries are rejected, never silently fixed.
- The catalog must be nonempty, and every gender it uses needs at least one voice
  profile in the active groups. The bundled catalog has 271 male and 234 female names.

Names are drawn from a shuffled **name bag** without replacement. Generation 0
issues each base name once, unchanged (`Alice`). When the bag is empty, the next
allocation starts a new generation, refills from the current catalog, and
reshuffles; generation *g* adds the suffix *g + 1* (`Alice2`, then `Alice3`, ...).
There is no suffix limit, and the letters-only rule means `Alice2` can never collide
with a configured name. Catalog edits take effect at the next automatic refill.
Clearing or pruning a binding never returns its name, so a name is used at most
once per generation within a state directory and [epoch](#explicit-reset-bags-reset---confirm).

The name is the only agent identifier: it appears in prefixes, captions, pane
titles, history, and errors. `context` still returns an `id` field, but only as a
temporary alias equal to `name`.

### Voice groups and voice profiles

Voices live in **voice profiles**, grouped into voice groups. Select one or more
groups at the **top of the file** (before any table headers):

```toml
active_groups = ["phonon"] # Or ["pocket_tts"], or both.

[voice_groups.phonon.voices.voice_juniper]
gender = "female"
phonon_voice = "Juniper"
eleven_labs_voice_id = "cgSgspJ2msm6clMCkdW9"
voice_approved = true

[voice_groups.pocket_tts.voices.voice_jean]
gender = "male"
pocket_tts_voice = "jean"
```

- Profile IDs (`voice_juniper`) are stable config keys, unique across all groups.
  They are not agent names and are never shown as speakers.
- `gender = "male"` or `"female"` is **required** and explicit; it is never
  inferred from the profile ID or a provider voice. Keep every provider's voice
  in a profile the same gender, so changing `[speech].mode` never changes how an
  agent sounds.
- Profiles contain only voice settings: provider voice fields, approval flags, and
  speed overrides. A `name` field (or anything else) is rejected; names come from
  the catalog.
- When a name is drawn, its gender selects a **matching-gender voice bag**: a
  shuffled queue of eligible profiles, refilled independently of the name bag.
  Each profile is used once per voice cycle before reuse, and **many agents may
  share a profile** at the same time, so the number of profiles never limits the
  number of agents.

Only profiles in `active_groups` are eligible for **new** assignments and appear in
voice review (including `--all`). Omitting `active_groups` selects all groups;
empty, unknown, or duplicate lists fail validation. Group activation changes are
reconciled into the voice bags on the next draw. The bundled groups are `phonon`
(24 profiles, one per conversational English Phonon voice, 12 male and 12 female,
with preserved ElevenLabs IDs) and `pocket_tts` (one profile per English preset
in Pocket TTS 4.0.1; three are commented out). Six other-language presets are not
included. Group selection and `[speech].mode` are independent: set both when
changing provider. Blank voices for the selected provider remain text-only.

**Each agent keeps a snapshot of its voice.** At allocation the selected profile is
deep-copied into the agent's binding (with local voice paths made absolute), along
with `voice_profile_id` for provenance. Delivery always uses that snapshot, never
the live config. Editing, moving, deactivating, or deleting the source profile
affects future agents only; existing agents keep their voice across lookups,
config reloads, and restarts until their binding is cleared. To give a pane new
voice settings, run `agent-comms context --clear` in that pane, then
`agent-comms context` (it also receives a new name). Never clear other panes'
bindings. Copying a profile does not copy external voice files or provider
voices: deleting those can still silence an existing agent. Global speech settings
(provider, mute, volume, and global defaults) still apply as before.

Legacy `[identities.ID]` and `[identity_groups.G.identities.ID]` tables are no
longer accepted; loading fails with conversion guidance (see
[Migration / cutover](#migration--cutover)).

Restart agent-comms after editing groups, profiles, names, or speech settings (see
above). Review decisions update the original nested TOML table without
flattening the file or modifying other groups.

### Voice review

Run `agent-comms voice-review` in an interactive terminal with a supported audio
player installed. It audits **voice profiles**, never generated agents, printing
each as `[i/n] PROFILE_ID (gender) — mode: voice`. It uses `[speech].mode`: Pocket TTS needs `pocket-tts`, Phonon needs `ptts`; ElevenLabs needs
`ELEVENLABS_API_KEY` and consumes API credits. Use `agent-comms voice-review --all`
to include approved profiles in the active groups. Samples play without robot effects or normalization,
but honor the configured speed. The prompt (`Do you like the PROFILE_ID voice?`) accepts:

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
voices are offered again. With nothing pending it prints `All active voice profiles
are approved.` Each yes/no is saved immediately to that profile's
`[voice_groups.G.voices.ID]` table, so quitting or Ctrl+C
keeps completed decisions. Blank voices are skipped without changes. Approval is
for this workflow only, not name/voice allocation or ordinary message playback.
Approvals are profile-scoped and survive `bags reset`.

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
history_size = 10 # Number of accepted messages to retain.

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

[voice_groups.phonon.voices.voice_juniper]
gender = "female"
eleven_labs_voice_id = ""
phonon_voice = "Juniper"
voice_approved = false
phonon_voice_approved = false
elevenlabs_speed = 1.1 # Optional per-profile override.
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

Each profile's `pocket_tts_voice` accepts a preset name, a local reference WAV,
or an exported `.safetensors` voice. Relative file paths resolve from the directory
containing `config.toml`, not the listener's working directory. Absolute paths
and `~/` paths also work. For example, store a clone in `agent-comms/voices/`:

```toml
[voice_groups.pocket_tts.voices.voice_mine]
gender = "female" # Match the recording's voice.
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

Agents already bound keep their voice snapshot; clear and re-resolve only the panes you
want to move to the Pocket group (they receive new names too). Restart existing reviews to pick up the changes.

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

Set `[speech] mode = "phonon"` and each profile's `phonon_voice` to a catalog
name. Blank/missing names keep that profile text-only in Phonon mode. The
bundled `phonon` group has 24 profiles with distinct conversational English voices;
the existing ElevenLabs approval flags do not represent Phonon auditions.
Keep the `eleven_labs_voice_id` fields: switching `mode` back to `"elevenlabs"`
restores them. Failures fall back to captions, **never to the other provider**.
`voice-review` auditions the provider selected by `mode`; `--all` includes approved voices.

Use `agent-comms restart watchers` after changing speech mode/settings, or
`agent-comms restart all` when also changing voice groups/profiles or listener code.
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
globally in `[speech]` or within each voice profile. All apply to `watch` and
`voice-review` and must be numbers from 0.7 to 1.2. Resolution order:

1. Profile's selected-provider speed (`phonon_speed`, `pocket_tts_speed`, or `elevenlabs_speed`).
2. Profile's legacy `speed`, if present.
3. `[speech]` selected-provider speed.
4. `[speech].speed` (legacy fallback), then 1.0.

For speech delivery, the profile values come from the agent's voice snapshot, so
editing a profile's speed affects only agents assigned afterwards.

ElevenLabs receives the multiplier in `voice_settings.speed`; Phonon and Pocket TTS use local
FFmpeg `atempo` processing without changing pitch. A provider's setting never
affects the other provider. The bundled config preserves the previous rates:
global 1.0 for all providers, and the `voice_toby` profile's override of 1.2 for ElevenLabs and Phonon.
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

Fill each profile's `eleven_labs_voice_id` with a voice available to your
ElevenLabs account (of the profile's gender) to enable ElevenLabs speech for
agents using it. Blank voice IDs deliberately remain text-only.
Agents have names and voices, not personalities. Profiles accept only voice
settings, so obsolete `personality` (or `name`) fields in older configs are
rejected at load rather than ignored.

Default locations:

| Platform | Config | Allocation state and history | Discovery and server lock |
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

### Allocation state

All name/voice allocation lives in one versioned JSON document,
`STATE/allocation.json`, guarded by the separate `allocation.lock`. It holds the
reset epoch, the name generation and remaining name bag (with genders), the
per-gender voice bags, every pane/shell binding with its voice snapshot, scan
times, and retired pane-title prefixes. Allocation, clearing, pruning, reset,
title bookkeeping, and message acceptance all serialize through that lock, and a
name, voice, and binding are committed together in one write. Each write is an
atomic replacement; the lock file is never replaced or deleted. An interrupted
allocation may consume a name the client never saw, but that name is never issued
to anyone else.

Send paths (`send`, and the listener's read-only binding checks) read the document
**without** locks, directory creation, or permission changes, so sandboxed agents
need no write access to the state directory.

The store **fails closed**: corrupt JSON, an unsupported version, or inconsistent
contents (for example a name both issued and still in the bag) produce an error
with recovery guidance instead of silently starting over. Pre-refactor
`pane-identities.json` / `bag.json` without an `allocation.json` count as legacy
state: `context`, `send`, and `context --clear-all` refuse to run until you
migrate with `bags reset --confirm` (see below). `bags status` reports legacy
state as `{"initialized": false, "legacy": true, ...}`.

Name uniqueness is scoped to **one state directory and one epoch**. Separate
installations, `--state-dir` overrides, deleting state, or restoring an older
backup can produce repeated names.

Check the current state with:

```sh
agent-comms bags status
```

It prints JSON with `epoch`, `generation`, `names_remaining`, `catalog_names`,
`voices_remaining` per gender, `bindings`, and the sorted bound `agents` names.

### Explicit reset (`bags reset --confirm`)

```sh
agent-comms bags reset --confirm
```

This administrative command is the **only** way to return to generation 0 or
change the epoch. In one transaction under the allocation lock it:

- generates a new opaque **epoch**, resets the name generation to 0, and refills
  and reshuffles the name and voice bags from the current config;
- clears **every** pane and shell binding (all cached contexts become invalid, and
  names may now repeat names issued before the reset);
- preserves message history, voice approvals, and the ElevenLabs rejection list.
  Already-accepted messages keep their recorded name, epoch, and voice;
- migrates legacy state: `pane-identities.json` and `bag.json` are moved into a new
  `STATE/legacy-backup-YYYYMMDD-HHMMSS-XXXXXX/` directory, and their old pane-title
  prefixes are remembered so titles are normalized on the next lookup;
- recovers from corrupt or unsupported state by moving `allocation.json` into the
  same kind of backup directory before starting fresh.

It prints `{"epoch": ..., "generation": 0, "cleared": N, "migrated": bool}` (plus
`backup` when files were moved) and a stderr reminder that agents must run
`agent-comms context` again. Without `--confirm` it refuses and explains the
consequences. It loads the config, so the config must be valid. Ordinary service
start/restart never resets anything.

The epoch is state-directory-wide, not an agent ID or secret, and does not change
on listener restarts or automatic name refills. Agents echo it on every send, so
a context cached before a reset is rejected even when the same pane receives the
same name again. The rejection returns fresh context as usual.

## Live voice controls

```sh
agent-comms audio toggle
agent-comms audio mute --seconds 60
agent-comms audio mute           # until explicitly unmuted
agent-comms audio unmute
agent-comms audio status         # JSON; no identity/config needed
agent-comms audio cycle-volume   # mute → 10% → 25% → 50% → 100% → mute
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

Voice volume starts at 100% and is shared across watchers. `cycle-volume`
cycles through permanent mute, 10%, 25%, 50%, and 100%. From 100%, the next
preset is actual mute: speech generation stops and `[Muted]` stays visible.
Cycling from any mute selects 10% and cancels its countdown. Volume presets
adjust ongoing speech playback gain and show `[Volume 25%]` for two seconds.
This does not change system volume. Restarting the listener resets voice volume to 100%.

The checked-in tuios config binds **Ctrl+A, Shift+M** to
toggle mute indefinitely and **Ctrl+A, Alt+M** to mute for 15 seconds.
Edit `--seconds 15` in its
`[[keybindings.command]]` entry to change that shortcut's duration. Commands run
in a temporary popup and never type into the focused agent or shell.

After installing this update, restart the existing listener and watchers once
to load the code. Later mute changes need no restarts. For the dock watcher,
run `tuios refresh-dock agent-comms` in each attached session.

## Pane name prefixes

`agent-comms context` also names its own pane `[NAME] Current terminal title`.
Each lookup refreshes the suffix from tuios's live terminal title, not the
previous custom display name. Missing, wrong, or stacked agent prefixes are
normalized to the current name. Recognized prefixes are any catalog name with an
optional generation suffix (`[Alice] `, `[Alice2] `, `[Alice13] `), the binding's
previous exact prefix, and prefixes retired by clears, pruning, or reset (including
legacy pre-refactor prefixes migrated by `bags reset`); unrelated bracketed labels in the terminal
title remain intact. Custom pane names are replaced by this normalized live title
on lookup; there is no full-name ownership tracking.

Names are custom tuios pane names, not terminal escape titles: they refresh only
on `context` and accepted pane-scoped `send` calls, not continuously or through
title-change subscriptions. Send refreshes run best-effort in the listener after
acceptance, independently of message delivery; naming failures never reject the
message. The listener verifies the pane's existing binding and live context,
coalesces concurrent refreshes per pane, and never allocates a name on send.
Clients still perform no state writes. Clear the
custom name with `tuios set-window -w PANE --name ''` to return to the shell's
title (the next context call prefixes it again). Naming errors warn on stderr
without breaking context's JSON or retrying denied requests.

## Delivery effects

Colored watchers render at 20 frames per second while a message is active.
A two-second trail of measured speech energy glows behind steady, bright caption
text across the entire bar. Only columns with audible energy receive the speaker's
name-colored background; quiet columns use the terminal's default transparent
background. There is no blanket dark fill. Lit cells use solid ANSI colors because
terminals do not support per-cell background alpha. Caption characters and spaces
remain intact, with no block bars or added waveform glyphs. The arrival flash uses
foreground brightness instead of reverse video.
The envelope combines the measured frequency bands of the final PCM audio after
robot effects and normalization, and follows playback time and live volume.
All watchers share the same bounded audio data, without extra synthesis or access
to temporary audio files. Silence, muted speech, preparation and unavailable
analysis leave the background transparent. Idle and offline output remain static.
Rendering uses only single-line text and ANSI SGR.

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
`protocol_version`, `token`, and `command`. The current protocol is **version 3**
(generated names, reset epochs, message IDs, conversation metadata, and the shared
presentation lifecycle); version 2 introduced distinct `tts`, unique message IDs,
persisted acceptance, sender context, and speech leases. Clients, listeners, and
watchers must all speak the same version: a mismatch fails with
`Unsupported discovery protocol version ... Restart the listener and watchers with
the same agent-comms version ('agent-comms restart all')`, and a listener rejects
requests from other versions. Discovery also advertises capability flags:
`prefix_policy: "explicit"`, `spatial_prefix`, `audio_control`, `pane_identity`,
`pane_identity_clear`, `shell_identity`, `name_identity`, `conversation_view`, and
`presentation_lifecycle`. Clients check the flags they need and ask for a restart
when one is missing.

`send` carries `name`, `epoch`, `msg`, optional `tts` (defaults to `msg`),
optional sender context and display metadata, and the caller's owner
(`pane_env` inside tuios, otherwise `shell_pid`); the listener rejects sends
without an owner. Successful replies include `ok: true` and the protocol version;
send replies add `message_id`, `warnings`, and `estimated_total_seconds`. A
name/epoch mismatch returns an error frame with `context_mismatch: true`.
Watch replies have `event: "state"`, `message` (null for idle, or a message with
`uid`, `id`, `name`, `msg`, `tts`, `voice_id`, conversation fields, and presentation
phase), and `lifecycle`: `{"timeline", "seq", "event", "message_id"}`, where
`event` is `start` (preparing), `display` (timed display begins), or `end`. Every
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
running listener. Set the top-level `history_size` in `config.toml` to a positive
integer (default 10), then restart the listener. This sets both retention and the
default number shown, printed oldest-to-newest with UTC acceptance timestamps and
sender names. When a message names another agent, or the sender explicitly chose
`--display-recipient user`, the heading shows it as `Alice → Ben` (or `Alice → Oli`).
Plain sends to the default user keep the plain `Alice` heading. Without a config file,
reading history defaults to 10 messages.
`--limit` accepts 1 through `history_size`; `--name`, `--epoch`, `--session`, and
`--json` are optional. `--name` matches the generated name, ignoring case; because names
can repeat after a reset, add `--epoch EPOCH` to select one epoch. `--id` is a
deprecated alias (printing a note) that matches the record's `id` field: the
generated name for new records, or the old identity ID for legacy records.
Legacy records are kept and displayed as before; they have no epoch. Filters apply
within the retained window, not a separate per-name/session history. Reducing the
size trims older messages on the next accepted send; increasing it cannot recover
messages already dropped.

For a live, event-driven log, use a watcher:

```sh
agent-comms watch --view log
agent-comms watch --view log --no-animation
```

The log watcher loads retained messages, then subscribes to the listener's shared
queue and presentation events. New messages slide in on acceptance; queued records
are labelled, and the current speaker highlights while preparing or speaking,
with a live playback progress bar. It never claims speech or generates audio.
It keeps the last retained records visible while offline, reconnects automatically,
and resynchronizes without replaying arrival animations. Like the other watchers,
it reloads after `agent-comms restart watchers` or `restart all`, responds to
terminal resizing, preserves scrollback, and exits cleanly on Ctrl+C.

`history` is one-shot output, including when the listener is offline. Its filters,
`--limit`, JSON, and raw output remain available. The old `--watch`,
`--refresh-time`, and history-specific `--no-animation` options are removed.
The log watcher displays the configured retained window; it does not use the
one-shot history filters.

Private `state/history.jsonl` contains **at most `history_size` entries**. Each acceptance
atomically replaces it with the newest retained messages, flushing before acknowledging
the send. A full queue or failed history write rejects the send. An OS-backed
lock coordinates writers; readers skip incomplete/corrupt rows. History
records acceptance, **not proof of display or playback**; a server crash can leave
accepted but undelivered messages in history. Full records include TTS text,
sender metadata, the reset epoch, the voice profile ID, the message ID, and any
conversation display metadata; they keep `id` equal to the sender's name for older
readers. No API key, authentication token, or speech lease is saved.
Older entries are dropped automatically; treat retained history as private data.
Messages sent before history support was installed cannot be recovered.

## ElevenLabs speech and timed captions

Watchers use **only** their `ELEVENLABS_API_KEY` environment variable for the API
key. No key is read from config, sent to the listener, written to history, or
included in diagnostics. Providing the key enables external ElevenLabs requests
for agents whose voice has a configured voice ID and an available local audio player.
The TTS text is sent to ElevenLabs; keep sensitive content out of announcements.

In ElevenLabs mode, the watcher shows `[12,345 | 42]` beside
the caption. The balance is the subscription's `character_limit` minus
`character_count` (floored at zero), refreshed in the background on startup,
every 60 seconds, and after a reported generation. It shows `?` if
the key is absent or subscription access fails; restricted keys need permission
to read subscription information. Usage checks never delay speech playback.
Per-message credits come from ElevenLabs' `character-cost` response header,
shared with all watchers through the speech lease; missing cost headers are
omitted rather than estimated. These are credits, not currency charges or an
overage spending allowance. Muted watchers can still show the balance. Other
speech modes make no billing requests and show no credit badge.
Restart the listener and watchers with `agent-comms restart all` to load this change.

`eleven_turbo_v2_5` matches `nvim/lua/features/voice/runtime.lua`'s Naia default.
Use `--tts` for speech-friendly spelling, punctuation, and supported markup such
as `<break time="0.3s" />`. Turbo does **not** support v3-only `[warm]`/`[excited]`
audio tags. Keep `--msg` plain, readable, and equivalent in meaning:

```sh
agent-comms context
# Inspect the returned JSON. Use its name, epoch, and exact prefix in the next tool call:
agent-comms send --name RETURNED_NAME --epoch RETURNED_EPOCH --msg "RETURNED_PREFIX Build finished. Tests passed." \
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
than permanently truncating the message. Long agent names are shortened to
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

## Conversation view

A silent, pane-sized view that shows who is talking to whom, alongside the normal
caption watcher:

```sh
agent-comms watch --view conversation
agent-comms watch --view conversation --no-animation --no-color
```

Run it in its own tuios pane: for example, split a narrow pane beside your agents
(or open one in a spare workspace) and start the command there. The default
`watch` (`--view captions`) is unchanged and remains the dock view.

- **Always silent.** It never claims, synthesizes, or plays speech; `--mute` is
  accepted but has no effect, and it does not change shared mute or volume. Keep a
  caption watcher (such as the dock) running for audio.
- **Same timeline.** It follows the listener's shared presentation lifecycle
  (`start`, `display`, `end`) and uses the same caption chunking and timing as the
  dock, so text advances in step with speech. It never dequeues, reorders, or
  times messages itself, and any number of views can be open.
- **Late join.** A newly opened view shows the current message and its progress,
  then future events. It does not replay history. After a listener restart it
  resynchronizes to the new timeline.
- **Layout.** A header bar shows the live status (`● speaking`, `◌ preparing`,
  `○ idle`) and a colored chip for the message `kind`. The requester stays on the
  left and the responder on the right, each in a framed avatar with their ASCII
  face, name, `◆ session` (the tuios session they last spoke from, or `human` for
  the user), and role. While someone speaks, a pulse travels along the connector
  toward the listener, and a speech bubble with a pointer opens on the speaker's
  side. A footer shows the exchange ID, its message count, the emotion, and a
  progress bar for the shared timeline. Participants slide in when a new exchange
  starts (`--no-animation` keeps the sides and emphasis without sliding). Narrow
  (under 48 columns) or short (under 14 rows) panes use a compact
  `Alice ▸ Ben  QUERY` layout with the speaker's face and session.
- **Sessions.** Each binding remembers the verified tuios session from its latest
  `context` or `send`, and new exchanges snapshot every agent participant's
  session, so the view never resolves sessions against live state.
- **Terminal.** It requires a terminal (it fails when stdout is redirected), draws
  on the alternate screen without clearing scrollback, redraws only on change or
  resize, and restores the terminal on exit. All text is sanitized.
  `--no-color` or `NO_COLOR` disables color. Like other watchers it reloads on
  `agent-comms restart watchers`/`all`, shows offline status, and reconnects.
  Listeners without the `conversation_view` and `presentation_lifecycle`
  capabilities show a restart notice.

Messages from senders that pass no metadata appear as the sender speaking to the
user.

### Send display metadata

These optional `send` arguments only change how a message is **displayed**:

| Argument | Values | Default for a new exchange |
| --- | --- | --- |
| `--display-recipient NAME` | Another agent's current generated name, or `user` | The configured user (`[user].name`) |
| `--role ROLE` | `requester`, `responder` | `requester` |
| `--kind KIND` | `instruction`, `query`, `statement` | `statement` |
| `--emotion EMOTION` | `neutral` `(o_o)`, `happy` `(^_^)`, `sad` `(T_T)`, `concerned` `(o_O)`, `curious` `(?_?)`, `excited` `(^o^)`, `frustrated` `(>_<)` | `neutral` |
| `--reply-to MESSAGE_ID` | A `Message ID:` printed by an earlier send | No reply link |

`--display-recipient` is the **displayed recipient only; it does not route or
deliver the message**, and it defaults to the configured user. The sender is
always the verified caller; display metadata cannot impersonate anyone, contact
another agent, authorize anything, or prove that the recipient read the message.
Use the normal agent-to-agent tools for actual delivery.

```sh
agent-comms send --name Alice --epoch EPOCH \
  --display-recipient Ben --role requester --kind query \
  --emotion curious --msg "Could you review this?"
# ...prints: Message ID: 0f3c...

agent-comms send --name Ben --epoch EPOCH \
  --reply-to 0f3c... --kind statement --emotion happy --msg "Reviewed; looks good."
```

- **Recipients** resolve against agents currently bound in the same state
  directory and epoch, case-insensitively, without allocating anything. `user`
  (any case) selects the configured user's written name. An unknown name falls back
  to the configured user and prints `agent-comms: warning: Unknown display
  recipient ...` on stderr; the message is still sent. Addressing yourself is
  rejected. When metadata needs the user and `[user].name` is not configured, the
  send fails with configuration guidance (plain sends without metadata still work).
  The resolved recipient is stored with the message and is not re-resolved later.
- **Emotion** is the speaker's presented expression. Any word of 1–32 letters,
  `-`, or `_` starting with a letter is accepted (case-insensitively); unlisted
  values show the neutral face.
- **Exchanges.** Each accepted message gets a unique message ID (not an agent ID).
  A message without `--reply-to` starts a new exchange whose ID is its own message
  ID, placing the sender on the `--role` side and the recipient opposite.
  Messages without a reply link are always separate exchanges, even between the
  same agents.
- **Replies.** `--reply-to` inherits the earlier message's exchange and
  participants; your role is inferred from your name, and the recipient defaults
  to the other participant, so either side can speak repeatedly. An explicit
  `--role` or `--display-recipient` must agree with the exchange. A reply is
  rejected (`Message was not queued.`) if the ID is malformed, unknown, or no longer
  queued, presenting, or retained in history (`history_size`); if it comes from
  before the last reset or has no conversation metadata; or if you are not a
  participant.

Message IDs and conversation fields are saved in history and returned by
`history --json`.

## Message prefix rules

Configure each situation independently in `config.toml`:

| Table | Situation | Default fields |
| --- | --- | --- |
| `[prefix.focused]` | Sender is the focused pane in the viewed workspace | None |
| `[prefix.same_workspace]` | Another pane in the viewed workspace | Name and relative position; suppressed if it is the sole agent |
| `[prefix.different_workspace]` | Sender is in another workspace of the viewed session | Name, session, workspace, and absolute position |
| `[prefix.different_session]` | Sender's session has no attached/viewing client | Name, session, workspace, and absolute position |

Classification checks session visibility before workspace and focus, so a pane
remembered as focused in a hidden workspace does not count as currently focused.
It uses tuios's `tui_attached` and current-workspace information. With multiple
clients, this reflects the view tuios reports for the sender's session; it does
not independently track which physical terminal the user is looking at.

Each table accepts:

- `fields`: any unique subset of `name`, `session`, `workspace`, `position`.
  Fields render in that fixed order. An empty array disables the prefix.
- `fallback_fields`: replacement fields when `position` was requested but
  usable geometry is unavailable. Defaults to name and session, except for the
  focused case, where it is empty. Unavailable values are omitted.
- `only_when_multiple_agents`: suppress the prefix when this is the only agent
  in its workspace. Defaults to true only for `same_workspace`.
- `omit_single_workspace`: omit the workspace label when tuios reports only one
  workspace in use. Defaults to true; false always includes a requested label.

Workspace labels use the workspace name when available, otherwise its number.
Position is relative to the focused pane for `same_workspace`, and absolute
within the sender's workspace for other situations. No geometry is fabricated.
Standalone agents outside tuios still have no required prefix.

For example, use name-only prefixes for other visible panes and name/session
prefixes for other sessions, omitting all workspace and position information:

```toml
[prefix.same_workspace]
fields = ["name"]
only_when_multiple_agents = false

[prefix.different_session]
fields = ["name", "session"]
```

These produce `Alice here.` and `Alice here, of the work session.` respectively.
To require a prefix even for the focused pane, set
`prefix.focused.fields = ["name"]`. Missing tables/keys use the defaults shown
in the shipped config. Unknown situations, keys, fields, duplicate fields, and
non-boolean flags are rejected. The earlier `[prefix] spatial = false` switch
remains supported as name/session defaults for non-focused cases; explicit
per-situation fields take precedence.

Restart the listener after changing these settings so context lookups and send
validation agree. Agents with a retained prefix receive fresh context through
the usual mismatch response and must use it on their retry. Prefix rules do not
change pane names or stored source metadata.

## Checked names on sends

Agents must initially call `agent-comms context` in a **separate tool call**,
read the JSON result, then compose both arguments using its returned name and
exact prefix and send with `--name RETURNED_NAME --epoch RETURNED_EPOCH`. Reuse
these details for later sends in the same pane, refreshing on a name/epoch/prefix
mismatch. Never inherit a name or epoch from another pane or conversation or hide
the initial lookup in a shell variable.
Never pass `--harness`, `--session-id`, or an identity to `context`: those
flags are rejected. Inside tuios, ownership is derived from the caller's pane environment.
Outside tuios, the listener derives ownership from the persistent parent shell.
The CLI supplies its process ID, never a chosen name or conversation ID.

```json
{"id":"Alice2","name":"Alice2","epoch":"3f9c0a7d1b2e4c68","prefix":"Alice2 here, to your top-right."}
```

`name` is the generated agent name; `id` is a temporary compatibility alias with
the same value, not a second identifier. `epoch` identifies the current
[reset epoch](#explicit-reset-bags-reset---confirm); it changes only on an explicit
`bags reset --confirm`, never on restarts or name refills. `prefix: ""` means no
fixed introduction is required. Context stays internal. Lookup uses read-only
`tuios session-info` and `tuios list-windows`, with two-second timeouts.
Incomplete environment, an unknown pane, or failed/forbidden context queries
produce an error, not a guessed empty prefix. No pane is focused or manipulated.

`--name` is the canonical send option. `--id` is accepted for now as a deprecated
alias for `--name` (it takes the generated name, not a voice profile or old
identity ID) and prints `agent-comms: --id is deprecated; use --name with the
generated name.` on stderr. Passing both is a usage error. Callers that omit
`--epoch` are rejected and receive fresh context, so older clients recover on
their next retry.

Name allocation is performed by the running listener through its existing
authenticated localhost API. The CLI checks its own live pane context first and
forwards only tuios pane/session/host/socket addressing, never arbitrary environment
variables or credentials. The listener verifies that pane again before allocating
or updating its title. Neither `context` nor `send` writes the client's state files:
`send` reads the atomically replaced allocation state (or requests the shell binding
from the listener) without creating directories, changing
permissions, or opening lock files. This supports Codex's workspace-write sandbox
without adding the application state directory to its writable roots. Localhost
access must still be permitted. Inside tuios the caller must retain its `TUIOS_*`
environment. Outside tuios the listener checks the caller’s parent shell; the CLI
needs no process-inspection permission.

After upgrading, restart the agent-comms listener once. Older listeners produce
an explicit restart message; there is no fallback to client-side state writes.
On macOS, tuios discovery checks the normal Homebrew executable directories when
launchd's minimal PATH cannot locate it. A found executable is never retried via
another path after a permission or query failure. Errors distinguish client pane
checks from listener name lookup and report missing executables separately
from access denials.
`context --clear-all`, `bags reset --confirm`, and `voice-review` remain explicit local maintenance commands
and still require write access to their state/configuration.

**Nothing is automatically inserted.** Put nonempty `prefix` once at the start
of both `--msg` and `--tts`, before markup. Do not add a second introduction.
With an empty prefix, introduce yourself using the returned name on your first
message (or when your name changes); otherwise go straight to the update.

`send` checks `--name` and `--epoch` against the caller's binding and rechecks spatial context before queueing. Missing,
stale, wrong, or duplicated introductions are rejected without queueing, history,
or speech. On mismatch, use the returned fresh context and recompose once. Never silently retry.
The listener re-verifies the name and epoch against the owner's binding under the
allocation lock and checks the submitted context snapshot too; this is a workflow
check, not authentication against clients that can falsify their own context. The
accepted message's voice comes from the binding's voice snapshot.

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
on-click = 'exec "$HOME/.local/bin/agent-comms" audio dock-click'
refresh = "push"
max-width = 80
```

Left-click the agent-comms dock component to mute speech across all watchers for
15 seconds. Each left-click restarts the countdown; captions continue, and speech
resumes automatically without replaying skipped announcements. Ctrl+left-click to
cycle through permanent mute, 10%, 25%, 50%, and 100%. Mute remains until another
audio action changes it; volume values appear for two seconds. Cycling from a
timed mute selects 10% and cancels the timer. `dock-click` uses tuios's `TUIOS_CLICK_BUTTON` (1 left) and ignores other
buttons. Tuios does not forward modifiers, so on macOS and Windows the command
checks whether Ctrl is held when it runs; keep Ctrl held through the click.
Other platforms retain plain left-click mute only. Reload tuios config with
`tuios config apply` after updating the hook.

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
logs, history, and name bindings.

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
history, and name bindings. Stop/install/start again after moving Python or the
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
inside the TUI. Empty sessions and title-generation requests do not consume names.
Each tool continuation and post-compaction agent-loop request receives the same
lookup instructions. Concurrent initialization is coalesced. A bounded
128-session cache stores **instructions only**, never a name, epoch, or prefix.
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

Inside tuios, `state/allocation.json` maps **daemon + pane UUID** to a binding
(name, gender, generation, voice profile ID, and voice snapshot) under the
allocation lock. The daemon key hashes the stable host/socket path (or named
server fallback), never a PID or secret token. Workspace/session moves, harness
switches, forks in the same pane, and process restarts reuse the binding.
Separate panes always receive distinct names; there is no roster limit, because the
name bag refills with suffixed names and voices may be shared.

On `context`, at most hourly per daemon, a complete successful read-only
`tuios list-windows --all --json` snapshot checks every session, including
detached panes. Confirmed missing panes get a seven-day grace period. A later
successful snapshot after that period removes the binding; a returning pane
clears its missing marker. Failed, forbidden, partial, or inconsistent snapshots
never remove anything. Only records for the currently queried daemon are pruned;
records for an unreachable/decommissioned daemon are retained for manual review.
Pruned names are retired, never returned to the bag; a pane returning after
pruning must look up its context again and receives a new name. There is no
early "pressure" reclamation any more: names are unbounded, so nothing needs to be
freed for a new pane.

Outside tuios, the same document stores **host + shell PID + process creation time**
under a separate shell namespace. The listener walks the calling process's ancestry
to the nearest persistent shell, skipping temporary `sh -c` / `zsh -lc` tool shells.
Repeated CLI calls and agent restarts under that shell reuse the name. Different
shells, nested interactive shells, and reused PIDs with a new start time have distinct
owners. No shell profile changes, tuios installation, or caller-selected identity
arguments are needed. Standalone context returns an empty spatial prefix; introduce
yourself with the returned name on your first announcement.

Shell discovery runs in the listener using `ps` on macOS/Linux or PowerShell CIM on
Windows. Clients only submit their process ID over the authenticated localhost API;
they do not inspect processes or write state. Standalone sends read the existing
binding through a `shell-binding` RPC (returning `name` and `epoch`) without
allocating. A listener without the `shell_identity` capability must be restarted
after upgrading.

The listener must be allowed to inspect process ancestry. Missing parent shells or
failed/denied process queries produce explicit errors; GUI launches with no shell
ancestor must instead launch the agent from a terminal. No harness/session fallback
is invented. Shell inventories use the same grace period as pane inventories,
scoped to the local shell namespace. Live shell bindings and
tuios bindings are never removed by a shell inventory. `context --clear` releases
the current shell binding; `--clear-all` clears both shell and tuios bindings.

When `TUIOS_ENV=1`, tuios ownership, live spatial prefixes, and pane-title features
retain precedence. Incomplete or failed tuios context checks remain errors; they
never silently switch to a shell identity.

Bindings depend only on their own voice snapshot, not on current config, so
editing or removing profiles never reassigns an existing agent. Corrupt or legacy
state fails explicitly (see [Allocation state](#allocation-state)). Allocation is
a single atomic write of the name, voice, and binding; a crash can consume a name
the client never saw but cannot expose partial JSON or reissue that name.
Child-agent-specific Codex hooks are not installed; the adapter targets SessionStart.

[communication-instructions.md](communication-instructions.md) is the single
template used by both adapters. It defines the communication rhythm:
open longer tasks, send useful progress, and **close every turn immediately
before the final text response**, even for short answers. It preserves the
native-voice exception and Claude Code readability guidance, requests editor
cards before the closing message, and covers safe quoting, secrets, existing
permissions, and continuing without retry spam when the listener is offline.

Configure the human user's name separately from agent names in `config.toml`:

```toml
[user]
name = "Oli"       # Written responses and --msg.
tts_name = "Ollie" # Pronunciation spelling for --tts; defaults to name.
```

Both harness plugins inject these names through `agent-comms instructions`.
Names are optional; without `[user]`, instructions use generic user references.
`[user].name` is also the default displayed recipient (the `user` selector) in the
[conversation view](#conversation-view); sends that use display metadata need it.
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
Restarting the listener does not fix that restriction. Both normal name
lookup and sending require localhost access; the SessionStart hook only supplies
instructions and does not allocate names.

Use Codex's normal approval flow for any additional access. Do not automatically
enable general networking, bypass the sandbox, auto-approve commands, or silently
switch transports. A separately authorized narrow messaging tool is another
possible integration, but is not installed by this version.

## Migration / cutover

Generated names replace the old static identities. This is a coordinated change
to config, state, and protocol (version 3), so the listener, watchers, CLI, and
injected instructions must move together. Nothing migrates automatically: the
new code refuses to run against old state until you explicitly reset it.

### Config

The bundled [config.toml](config.toml) is already converted: every former identity
is now a voice profile under `[voice_groups.GROUP.voices.PROFILE_ID]` with an
explicit `gender`, its provider voices, approvals, and speed overrides intact, and
the old name and ID recorded in a comment on the table header:

```toml
[voice_groups.phonon.voices.voice_trevor] # formerly Will (ag_376b7d0a)
gender = "male"
voice_approved = true
phonon_voice = "Trevor"
eleven_labs_voice_id = "bIHbv24MWmeRgasZH58o"
```

`names_file = "names.toml"` was added at the top, and the rejection list and
`active_groups` were kept. To convert a **custom config** by hand, for each
identity table:

1. Rename the table: `[identity_groups.G.identities.ag_x]` becomes
   `[voice_groups.G.voices.PROFILE_ID]`; a flat `[identities.ag_x]` must move into a
   group, such as `[voice_groups.default.voices.PROFILE_ID]`. Pick a stable,
   descriptive profile ID (letters, digits, `_`, `-`), unique across all groups.
2. Add `gender = "male"` or `gender = "female"` matching the voice itself (check
   ElevenLabs `labels.gender` and the Phonon/Pocket voice), not the old display name.
3. Delete the `name` line (and any obsolete `personality`). Optionally keep the old
   name and ID in a `# formerly NAME (ag_...)` comment.
4. Add `names_file = "names.toml"` before the first table and copy or create a
   name catalog. Make sure each gender in the catalog has an active profile.

Loading a config that still has `identities` or `identity_groups` fails with
guidance pointing here.

### Live installation

1. **Merge the branch** into the checkout the service runs from.
2. **Restart everything:**

   ```sh
   agent-comms restart all
   ```

   This restarts the listener service, then signals every watcher (dock and
   conversation views) to re-exec into the new code. Until this step, old and new
   components refuse to talk to each other with restart guidance. Context and
   send fail with legacy-state guidance until step 3.
3. **Reset once to migrate state:**

   ```sh
   agent-comms bags reset --confirm
   ```

   This moves `pane-identities.json` and `bag.json` into
   `STATE/legacy-backup-*` (on macOS, under
   `~/Library/Application Support/agent-comms/state/`), starts a new epoch at
   generation 0, and clears all bindings. History and voice approvals are kept;
   old history records keep their old identity IDs and have no epoch. Old
   `[Name] ` pane-title prefixes are recognized and replaced on each pane's next
   lookup. Do not delete the backup until you are satisfied.
4. **Agent sessions.** New agent sessions receive the updated instructions
   (`--name`/`--epoch`) automatically. Already-running sessions keep their old
   instructions until reloaded or resumed (`/reload` in OpenCode; Codex picks them
   up at its next SessionStart), but they still recover on their own: their next
   send with a stale `--id`/name or no epoch is rejected with
   `Fresh context: {"id":...,"name":...,"epoch":...,"prefix":...}`, and retrying
   with that name and epoch succeeds. `--id` keeps working as a deprecated alias
   for `--name`.

Check the result with `agent-comms bags status`; the first `context` in each pane
allocates its new name.


### Named tree watcher and persistent roles

`agent-comms role 'worker'` sets your own display role; `role --clear` removes it.
Roles appear in conversation and tree views, independently of `send --role`.
Publish a JSON node array with `agent-comms tree set RUN --file tree.json`, then
open `agent-comms watch --view tree --tree RUN` in a terminal. Example array:

```json
[{"id":"root","agent":"Dennis"},{"id":"group","parent":"root","label":"engine"}]
```

Use actual current identities. Only the creator can replace/delete a named tree;
`tree show RUN` inspects it and `tree delete RUN` removes it. Nodes use stable
IDs, optional parent IDs, labels, and optional agent names. The silent watcher
shows pending counts, active speaker/recipient highlights and directed arrows.
External participants appear temporarily. State survives listener restarts;
bag reset clears it. Restart the listener after upgrading to enable this feature.
See [communication instructions](communication-instructions.md) for limits and usage.

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

### Watcher navigation, queue previews, and history

Click an agent name or role in the tree watcher to focus that agent's verified
pane. Cross-session targets also switch the client to the target session.
Grouping and info rows do not navigate. The watcher resolves current identity
bindings and checks that the exact pane still exists; unavailable destinations
and denied operations appear in the header. Navigation requires the watcher's
normal tuios read, focus, and session-switch permissions and never types into an
agent pane. Terminal mouse reporting is enabled only while the tree is open and
restored on exit. Terminal emulators may use Shift-click for text selection.

The conversation watcher includes an **Up next** panel when messages are queued:
FIFO ordering, pending count, sender/recipient, estimated wait, and silent text
previews. Press **Q** to expand/collapse the panel. The first five messages have
bounded previews (up to 400 characters each); additional messages still count
in the queue total. Waits count down and are estimates, since speech preparation
and playback can vary. The panel follows the shared queue without claiming speech.

`agent-comms history` now groups chronological messages by local date, with
speaker/role/recipient headings, subdued session metadata, and wrapped paragraphs.
`watch --view log` keeps a fixed header and follows listener events. Newly arriving
messages slide in from the right; initial loads, reconnect snapshots, and terminal
resizes do not trigger arrival animations. Queue labels, speaker highlights, and
playback progress react to the same lifecycle as the other watchers. Use
`watch --view log --no-animation` for immediate arrivals. `history` stays a
standard one-shot log with filters, JSON, and raw output.
