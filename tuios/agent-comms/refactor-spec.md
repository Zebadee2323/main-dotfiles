# Agent Comms: Recommended Refactor Specification

Status: proposed design, not an implementation.

This specification expands `refactor-work.md`. It separates agent names from
voice profiles, defines durable allocation and reset semantics, and adds a live
history view. The original proposal remains unchanged.

## 1. Goals

- Make the generated agent name its sole public agent identifier.
- Configure names independently of voices.
- Assign matching name/voice genders without limiting concurrent agents to the
  number of configured voices.
- Preserve assignments and name uniqueness across listener and machine restarts.
- Preserve voice groups, active-group selection, provider settings, and approvals.
- Provide an explicit administrative reset with safe stale-context handling.
- Add live history viewing without changing history retention.
- Add a silent, pane-sized conversation watcher that visualizes the speaker,
  displayed recipient, exchange roles, and expression on the shared timeline.

Uniqueness is scoped to one state directory and one explicit-reset epoch. Names
are not globally unique across independent installations, state deletion, or
restoring an older backup.

## 2. History CLI

`history --limit` already exists. Retain it rather than introducing a second
implementation. The configured `history_size` controls retention; `--limit`
controls how many retained, matching records are displayed.

```sh
agent-comms history --limit 10
agent-comms history --watch
agent-comms history --watch --limit 10 --refresh-time 0.5
```

### Behavior

- `--watch` immediately displays a snapshot, then polls for changes.
- `--refresh-time <seconds>` defaults to `1.0`; accept only positive, finite
  numbers. Reject it when `--watch` is absent.
- Preserve existing identity/session filters, ordering, formatting, and color
  options. Display the latest matching records in chronological order.
- Keep existing `--limit` validation: an integer from 1 through `history_size`.
- Redraw the view in place only when the displayed content or terminal dimensions
  change. Avoid clearing terminal scrollback.
- Require a terminal for watch mode. Initially reject `--watch` with `--json`,
  `--raw`, or redirected stdout; streaming machine output is a separate feature.
- Restore terminal state and exit cleanly on Ctrl-C.
- Continue working while the listener is offline. An absent history file is an
  empty view; atomic file replacement must not interrupt polling.
- Watching never generates speech or submits messages.

## 3. Configuration

### 3.1 Name catalog

Add a separate `names.toml`, referenced from the main configuration by a path
resolved relative to that configuration file.

```toml
# config.toml
names_file = "names.toml"
```

The name catalog contains names and gender only. Array membership supplies the
gender; names have no configured IDs, voice references, or other metadata.

```toml
# names.toml — illustrative subset
male = ["Adam", "Ben", "Leo"]
female = ["Alice", "Clara", "Maya"]
```

- Ship hundreds of distinct, easy-to-pronounce names across both lists.
- Supported gender values are `male` and `female`.
- Initially restrict base names to ASCII letters, starting with an uppercase
  letter. No digits, whitespace, punctuation, or generated suffixes are allowed.
- Require case-insensitive uniqueness across the entire catalog, not merely
  within each gender. Do not silently normalize or deduplicate invalid entries.
- Reserve `user` case-insensitively for the conversation view's configured-user
  recipient selector; reject it as a base name.
- Require a nonempty catalog. Every represented gender must have at least one
  eligible voice profile in the active groups.

The letters-only rule makes suffix concatenation unambiguous: a configured
`Alice2` cannot collide with generation-one `Alice`.

### 3.2 Voice profiles

Replace static identity tables with voice-profile tables, retaining group
selection semantics. For example:

```toml
active_groups = ["phonon"]

[voice_groups.phonon.voices.voice_trevor]
gender = "male"
phonon_voice = "Trevor"
eleven_labs_voice_id = "provider-voice-id"
voice_approved = true
```

- Voice-profile IDs remain stable configuration keys, unique across groups.
- Profiles retain provider-specific voice names/IDs, speed overrides, approval
  flags, and existing supported voice settings.
- Profile IDs are not agent identifiers. A profile can serve many assignments.
- Gender describes the configured profile and must be explicitly supplied, not
  inferred from its name or provider ID.
- Validate matching provider genders during catalog preparation. A profile
  should not change perceived gender when the speech provider changes.
- Keep existing behavior for missing provider voices, including text-only
  delivery where currently supported; this refactor does not redefine it.
- Voice review operates on profiles, never on generated agent assignments.

## 4. Runtime Identity Model

An assignment contains:

```text
name:              Alice2
base_name:         Alice
gender:            female
name_generation:   1
voice_profile_id:  voice_clara
voice_profile:     complete immutable copy of the selected profile
owner:             existing verified pane/shell owner key
```

These are runtime assignment fields, not additional name-catalog metadata.

- `name` is both the public identifier and display name. No new opaque per-agent
  ID is introduced.
- Continue deriving ownership from the actual caller's pane or persistent shell.
  A caller-supplied name asserts an expected assignment; it cannot select one.
- Return the existing assignment on repeated context lookups and after restart.
- At allocation, deep-copy all selected voice-profile data into the assignment,
  including gender, provider voice IDs/names, speed overrides, approval flags,
  and all other supported profile settings. Persist this snapshot with the
  binding; do not retain mutable references to configuration objects.
- Retain `voice_profile_id` for provenance only. Runtime validation and speech
  delivery use the embedded snapshot, never a lookup of that ID in live config.
- Preserve the snapshot unchanged across context lookups, configuration reloads,
  and service restarts until the binding is explicitly cleared. Editing, moving,
  disabling, or deleting the source profile must not invalidate or revoice an
  existing assignment.
- Resolve configuration-relative voice asset paths to absolute paths before
  storing the snapshot. Copying profile data does not copy external voice files
  or guarantee continued availability of provider voices; removing those assets
  or external voices can still prevent delivery.
- Snapshot profile-level settings only. Global speech controls retain their
  existing semantics, including provider selection, mute, and volume. Global
  defaults still apply where the snapshot has no profile-level override.
- Existing garbage collection may remove obsolete owner bindings, but must never
  return their names to the current name bag.

Separate public identity lookup from voice configuration lookup throughout the
code. Generated names are not keys in the static voice configuration.

## 5. Bag Allocation

### 5.1 Name bag

- Use one global name generation, not independent male/female generations.
- First initialization creates generation `0` and shuffles the full catalog.
- Each new assignment consumes one name without replacement.
- On the next allocation after the bag becomes empty, increment generation,
  refill from the currently loaded catalog, and shuffle again.
- Generation `0`: return the base name unchanged, such as `Alice`.
- Generation `g > 0`: return `base_name + str(g + 1)`, such as `Alice2` or
  `Alice3`. Do not impose a small fixed suffix limit.
- Clearing a binding does not return its name, decrement generation, or trigger
  a refill.

### 5.2 Voice bag

Treat the voice bag as gender-partitioned shuffled queues over eligible profiles.
This is one logical voice pool with a queue for each supported gender.

1. Draw the next name from the global name bag.
2. Read that name's gender.
3. Draw from the matching-gender voice queue.
4. If that queue is empty, refill and shuffle it independently of the name bag.
5. Copy the complete selected voice profile into the assignment and persist it
   with the owner binding and both updated bags together.

Voice reuse is allowed even while another live agent uses the same profile. Each
eligible profile is used once per matching-gender voice cycle before reuse.
Voice cycles do not advance the name generation.

Reject invalid configuration before allocation if a possible name draw has no
eligible matching voice. Never consume a name and then leave a partial binding.

## 6. Persistence and Concurrency

Use a versioned, durable allocation store containing:

- Reset epoch.
- Current name generation and remaining name entries, including their genders.
- The catalog snapshot needed to finish the current name generation.
- Remaining voice-profile queues and their eligible-profile membership.
- Owner bindings, their complete voice-profile snapshots, and existing
  lifecycle/title bookkeeping.

Recommended implementation: a single allocation-state JSON document protected
by one lock, written through crash-safe atomic replacement. An equivalent
transactional store is acceptable.

- Allocate a name, voice, and owner binding in one transaction.
- Serialize reset, clear, allocation, and binding/title updates through the same
  state ownership mechanism. Avoid competing writers that lose newer fields.
- Report success only after the allocation is durably committed.
- Read-only send paths must retain current sandbox-compatible access behavior;
  they must not need to create directories, change permissions, or acquire
  writable lock files.
- Concurrent context calls for one owner return the same committed assignment.
- Concurrent allocations for different owners cannot issue duplicate names.
- An interrupted allocation may consume a name without the client seeing it;
  it must never issue that name to a different owner afterward.
- Corrupt, unsupported, or inconsistent state fails explicitly with recovery
  guidance. Never silently recreate generation `0` or restart a name cycle.

### Configuration changes

- Retain the current name-generation snapshot until it is exhausted. Additions,
  removals, or gender changes take effect on the next automatic name refill.
- Validate the remaining snapshot against active voice genders when loading
  configuration; fail clearly if it can no longer be allocated safely.
- Changes to active voice groups affect new assignments only. Reconcile voice
  queues by removing ineligible profiles and adding newly eligible profiles once,
  preserving already-consumed entries for otherwise unchanged cycles.
- Existing assignments retain their embedded snapshots even when their source
  profile is edited, moved to an inactive group, or removed entirely. Do not
  require source-profile membership when loading or validating these bindings.
- Profile edits and removals affect future allocations only. Removing a profile
  requires no binding cleanup; assigning new settings to an existing owner
  requires explicitly clearing its binding and allocating a fresh identity.
- Validate snapshots against their persisted schema, not against current profile
  contents. Schema migrations must preserve the saved voice data rather than
  replacing it from live configuration.

## 7. Clear, Refill, and Reset Semantics

These operations have different purposes and must not be interchangeable.

### Clear current binding

Keep `context --clear`: release the caller's binding without returning its name
to the bag. Its next context lookup allocates a fresh name.

### Clear all bindings

Keep `context --clear-all`: clear owner bindings only. Preserve bag contents,
generation, and reset epoch. Previously issued names remain spent.

### Refill

Retire the ambiguous `context --refill` operation with a clear CLI error and
guidance. Names refill automatically on exhaustion; premature refills could
otherwise reuse names within a generation. Independent manual voice reshuffling
is outside the initial scope.

### Explicit reset

Add an administrative command:

```sh
agent-comms bags reset --confirm
```

- Require explicit confirmation through `--confirm`.
- Reset name generation to `0`, refill all bags from current configuration, clear
  all owner bindings, and generate a new reset epoch in one transaction.
- Preserve message history and voice approvals/rejection lists.
- Serialize reset with allocation and message acceptance. Already-accepted
  messages retain their recorded name and voice; reset does not rewrite them.
- Report that cached contexts are invalid and that names may now repeat.
- Do not allow ordinary service start/restart to perform this reset.

## 8. Protocol and Stale Contexts

Name-only assertions cannot distinguish an old `Alice` from a newly allocated
`Alice` after explicit reset. Preserve the name-only public agent identifier,
but add a shared reset-epoch assertion to cached context and send requests.

```json
{
  "name": "Alice2",
  "epoch": "opaque-reset-epoch",
  "prefix": "Alice2 here"
}
```

```sh
agent-comms send --name Alice2 --epoch <returned-epoch> --msg "Ready."
```

- The epoch is state-directory-wide, not an agent ID or authentication secret.
- It changes only on explicit reset, not on service restart or name refill.
- Require both the expected name and epoch to match the verified owner's current
  binding before accepting a message.
- On mismatch, reject without queueing and return fresh context through the
  existing recovery path.
- Make `--name` the canonical option. Retain `--id` temporarily as a deprecated
  alias accepting a generated name, not a voice-profile ID. Reject both together.
- A temporary context `id` compatibility field may equal `name`; it must not
  introduce a second identifier. Clients still need updating to supply epoch.
- Update injected communication instructions and all maintained callers together.
- Never treat the asserted name, epoch, or voice-profile ID as caller authority.

If avoiding even a shared epoch field is a hard requirement, explicitly accept
that resets cannot reliably reject stale contexts when names repeat. This spec
recommends the epoch rather than that weaker guarantee.

## 9. Message Records and Presentation

- Record the generated name, reset epoch, voice-profile ID, and resolved speech
  settings needed by downstream delivery at message acceptance. Derive profile
  settings from the assignment's embedded snapshot, never from the current
  source-profile configuration.
- Keep accepted messages independent of later name, profile, or reset changes.
- Distinguish records across resets using `(epoch, name)` internally. Historical
  display can remain name-first.
- Use generated names consistently in prefixes, captions, pane titles, logs,
  filtering, and CLI errors.
- Add `history --name`; retain `history --id` as a deprecated alias. Name filtering
  can match multiple epochs; add `--epoch` for disambiguation.
- Preserve legacy history records and rendering; do not invent epochs or remap
  old records to new assignments.
- Ensure title-prefix replacement handles numbered names and preserves unrelated
  user title labels.
- Store the conversation metadata defined below with accepted messages so live
  views and history do not need to resolve participants against current config.

## 10. Conversation Watcher

### 10.1 Purpose and command

Add a pane-sized visual conversation view alongside the existing caption view:

```sh
agent-comms watch --view conversation
```

- Keep the current watcher view as the default for compatibility.
- The conversation view is always silent: it neither synthesizes nor plays audio.
- It observes the same accepted-message queue and authoritative presentation
  timeline as the existing watcher; it does not consume messages destructively,
  reorder them, or maintain a second playback queue.
- `--mute` is redundant in this view and may be accepted as a no-op. It must not
  mute another view or change global audio controls merely by subscribing.
- This is visualization, not agent-to-agent dispatch or a new messaging transport.

### 10.2 Send metadata

Add these optional arguments to `agent-comms send`:

| Argument | Accepted values | Default for a new exchange |
| --- | --- | --- |
| `--display-recipient NAME` | Current generated agent name, or reserved `user` | Configured user |
| `--role ROLE` | `requester`, `responder` | `requester` |
| `--kind KIND` | `instruction`, `query`, `statement` | `statement` |
| `--emotion EMOTION` | Supported expression name | `neutral` |
| `--reply-to MESSAGE_ID` | Recognized accepted-message ID | No reply link |

`--display-recipient` help text must explicitly say:

> Displayed recipient only; does not route or deliver the message. Defaults to
> the configured user.

Do not introduce `--target` or `--dir` aliases. Use `--kind` instead of `--type`
and `--emotion` instead of `--sentiment` consistently in CLI, records, and docs.

Illustrative new-exchange command, in addition to required sender assertions:

```sh
agent-comms send --name Alice --epoch <epoch> \
  --display-recipient Ben --role requester --kind query \
  --emotion curious --msg "Could you review this?"
```

The sender is always derived from the verified caller binding. Display metadata
cannot impersonate a sender, authorize an action, contact another agent, or
prove that the displayed recipient received or read anything. Existing actual
agent-to-agent communication tools remain responsible for delivery.

### 10.3 Recipient resolution

- Reserve `user` as an explicit recipient selector; it is not an allocatable
  agent name. Resolve it to the configured user's written name, not TTS spelling.
- Resolve agent recipients against current assigned identities in the same state
  directory and reset epoch. Do not allocate an identity as a side effect.
- Missing or unrecognized recipient names fall back to the configured user.
  An explicitly supplied unknown name produces a warning in CLI results/stderr,
  but does not reject an otherwise valid new message.
- Persist the requested selector, resolved participant kind (`agent` or `user`),
  name, agent epoch where applicable, and whether fallback occurred. Do not
  re-resolve an accepted recipient when a binding disappears or reset occurs.
- A configured user name is required whenever user fallback is needed; otherwise
  fail with actionable configuration guidance rather than inventing a name.

### 10.4 Exchange roles and reply linkage

Distinguish the current speaker from their stable role within an exchange:

```text
Request: Alice -> Ben    Alice stays left; Ben stays right; Alice speaks.
Reply:   Ben -> Alice    Alice stays left; Ben stays right; Ben speaks.
```

- The requester is anchored left and the responder right. These labels describe
  layout roles, not permissions or delivery direction.
- For a new exchange, place the sender on the side selected by `--role` and the
  resolved recipient on the opposite side. A standalone responder message is
  allowed; its recipient is displayed as requester without implying a real
  earlier request.
- Assign each accepted message a unique message ID and return it in the send
  acknowledgement. This is not another kind of agent ID.
- A new exchange uses its first message ID as its exchange ID. Persist the
  requester and responder descriptors, exchange ID, speaker role, and optional
  reply-to ID with each message.
- `--reply-to` inherits the referenced message's exchange and participant
  snapshots. Infer the role from the verified sender, and default the recipient
  to the opposite participant. Either participant can speak repeatedly; the
  argument does not mechanically toggle sides.
- If explicitly supplied, role and resolved recipient must agree with the
  inherited exchange. Reject conflicting metadata, a sender outside the exchange,
  self-addressed exchanges, unknown/expired reply IDs, or cross-reset reply links.
  Do not silently start a different exchange or apply recipient fallback to a
  conflicting linked reply.
- Retain reply lookup metadata while messages are queued/playing and for messages
  retained in history. Beyond that bounded window, return an explicit expired
  reply error. Do not introduce unbounded conversation storage implicitly.
- Unlinked messages form independent exchanges, even between the same names.
  Do not infer conversation membership from participant names alone.

Example reply; role and displayed recipient are inherited:

```sh
agent-comms send --name Ben --epoch <epoch> \
  --reply-to <alice-message-id> --kind statement \
  --emotion happy --msg "Reviewed; looks good."
```

### 10.5 Visual presentation

- On entering an exchange, slide the requester in from the left and responder
  from the right. Label both with their snapshotted names.
- Keep participants in place across chunks and consecutive messages in that
  exchange. Changing the speaker changes emphasis, not participant positions.
- Place the active text nearer its speaker, within a stable reserved text region
  so left/right turns do not cause the entire pane to jump.
- Reuse the existing caption chunking and timing rather than independently
  splitting text or displaying the full message early.
- Show `kind` as a small label; do not infer emotion or delivery semantics from it.
- Display a small ASCII expression near the active speaker. Initial vocabulary:
  `neutral`, `happy`, `sad`, `concerned`, `curious`, `excited`, `frustrated`.
  Define a deterministic icon for each and fall back to neutral for unknown,
  bounded-length emotion values. Do not interpret emotion text as terminal markup.
- `emotion` describes the speaker's presented expression, not a classification
  of the recipient or an inferred emotional state.
- Support reduced motion with `--no-animation`, preserving sides and emphasis
  without sliding. Resize safely; use a compact labeled layout in narrow panes.
- Sanitize all labels and content using existing terminal-safety rules. Restore
  terminal state on exit and do not clear scrollback unnecessarily.

### 10.6 Shared timing and lifecycle

- Establish one authoritative presentation lifecycle per queued message, shared
  by caption and conversation renderers: start, chunk/progress, end/cancel.
  Refactor existing watcher-owned timing if needed rather than assuming these
  events already exist.
- Audio-capable delivery remains responsible for speech generation/playback.
  Silent subscribers must never trigger audio work, duplicate playback, or
  compete for playback ownership.
- When audio plays, publish its actual presentation timing. When muted, text-only,
  or no audio-capable watcher is present, use one shared text-duration fallback.
  Do not create separate timers per subscriber.
- Muting or joining/leaving a view must not restart an already-started message.
  Audio attaching mid-message waits for a subsequent message rather than
  replaying the current one from its beginning.
- Late subscribers receive the current message and progress snapshot, then future
  events; they do not replay all retained history. Deduplicate reconnect events by
  message ID and lifecycle position. After an authoritative timeline restart,
  resynchronize to its current snapshot rather than replaying stale local state.
- Preserve queue ordering and cancellation behavior. Multiple silent views must
  not change timing, drop messages, or introduce extra lifecycle events.
- Capability checks must reject incompatible listeners with upgrade guidance.

## 11. Migration

This is a coordinated schema/protocol change, not an in-place rename of the
existing bag function.

1. Convert existing identity groups into voice groups. Retain provider settings,
   approvals, rejection lists, and group membership. Explicitly supply profile
   genders; do not infer them automatically.
2. Add and validate the separate name catalog.
3. Detect legacy allocation state and require an explicit migration/reset action.
   Do not silently initialize the new allocator over existing state.
4. On explicit migration, preserve legacy state in a backup, invalidate old
   bindings, and initialize a new epoch at generation `0`.
5. Update listener, clients, plugins, injected instructions, and protocol capability
   checks together. Mixed incompatible versions must fail with upgrade guidance.
6. Preserve legacy history without implying uniqueness between old and new names.

Review allocation, pane/shell bindings, message validation, prefix generation,
voice review/config editing, history, watcher speech delivery, and title updates.
All currently assume some relationship between static identities and voices.

## 12. Acceptance Criteria

### History

- Existing one-shot history behavior and filters remain intact.
- Watch shows new accepted records without duplicate redraws and survives atomic
  history replacement, an absent file, and an offline listener.
- Interval validation, terminal resizing, unsupported output combinations, and
  clean terminal restoration are covered.

### Allocation

- Hundreds of configured names validate, with duplicates and invalid suffix-like
  names rejected.
- Every assigned name/voice pair has matching gender.
- Each base name appears once per generation; subsequent generations produce
  `Name2`, `Name3`, and so on.
- Gender-partitioned voice cycling never caps concurrent assignments.
- Bindings, remaining queues, and generation survive service restart.
- Clearing bindings and pruning stale owners never recycle names.
- Concurrent allocation and injected write failures cannot produce duplicate
  committed names or partial name/voice/binding state.
- Config edits follow the catalog-snapshot and voice-reconciliation rules.
- Assignments contain a complete deep-copied voice profile. Editing, disabling,
  moving, or deleting its source profile leaves existing assignments usable with
  unchanged profile settings, including after restart; future allocations use
  the updated eligible configuration.
- Configuration-relative voice paths remain correctly resolved in snapshots,
  and snapshot loading does not require the source profile to exist.
- Corruption fails closed instead of resetting counters.

### Reset and integration

- Only explicit reset returns generation to `0` and changes epoch.
- Cached pre-reset sends are rejected even if the same owner receives the same
  name after reset.
- Spoofed caller names cannot select another owner's assignment.
- Voice approvals remain profile-scoped, and accepted messages retain their
  original delivery configuration.
- Generated names, including suffixes, work in titles, prefixes, history filters,
  captions, and maintained client integrations.
- Legacy state/version mismatches produce actionable migration errors.

### Conversation watcher

- Existing send callers work without visual metadata; existing watcher behavior
  stays the default.
- Missing/unknown recipients use the configured user, with warnings for explicit
  unknown names. Accepted recipient snapshots survive edits and resets.
- Display metadata never routes messages or overrides verified sender ownership.
- Requester remains left and responder right when the speaker changes; text and
  expression follow the active speaker without replaying entry animation.
- Reply links inherit exchange participants and validate explicit overrides;
  unknown, expired, cross-reset, or unrelated-sender replies fail clearly.
- Message IDs and exchange metadata survive history serialization; bounded reply
  lookup does not accidentally become an unbounded archive.
- All kinds, supported emotions, unknown-emotion fallback, safe text rendering,
  narrow panes, resizing, and reduced-motion behavior are covered.
- Conversation-only operation never generates/plays audio and still advances the
  queue using shared fallback timing.
- Caption/audio and conversation views stay aligned; multiple observers, mute
  changes, reconnects, late joins, cancellation, and timeline restart do not
  duplicate playback or independently advance messages.
- Voice-profile snapshots continue to supply audio delivery regardless of which
  visual views subscribe.

## 13. Suggested Delivery Order

1. Add history watch independently; retain existing limit support.
2. Introduce name/voice schemas, validation, and explicit config conversion.
3. Implement the transactional allocator and owner-binding store.
4. Update context/send protocol, reset handling, and client integrations together.
5. Update history, voice review, presentation, documentation, and migration tests.
6. Add message IDs, visual send metadata, recipient snapshots, and reply linkage.
7. Establish shared presentation lifecycle events, then add the silent conversation
   renderer and its integration tests. Do not ship an independently timed queue.

No implementation or migration should run merely because this specification was
added.
