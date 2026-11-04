# agent-comms: the new name and voice system

This note covers what changed in agent-comms between the previous version
(commit `3a18735`) and the current one (`b75550a` and `d91494d`), and why the new
design is better, safer, and more fun.

## The short version

| | Before | Now |
| --- | --- | --- |
| Who an agent is | A fixed identity from `config.toml` (`ag_aca24036` = "Tarquin" + one voice) | A generated name (`Dennis`, later `Dennis2`), plus a voice picked separately |
| How many agents at once | At most one per configured identity (24 with the `phonon` group) | No cap: 1,271 names, then numbered rounds (`Alice2`, `Alice3`, …) |
| Voices | Fixed to one identity each | Voice profiles with an explicit gender; one profile can serve many agents |
| Sending | `send --id ag_aca24036` | `send --name Dennis --epoch 6a36…` (`--id` still works, but is deprecated) |
| Saved state | Two files (`pane-identities.json`, `bag.json`) with separate locks | One versioned `allocation.json`, written atomically under one lock |
| Starting over | `context --refill` (could reuse names that were still in use) | `bags reset --confirm`: a new epoch that makes every cached context stale |
| Watching | The caption line in the dock | Captions, plus a silent `watch --view conversation` pane and `history --watch` |

## What changed

### 1. Names and voices are separate

Before, each table in `config.toml` tied a name to a voice:

```toml
[identity_groups.phonon.identities.ag_aca24036]
name = "Tarquin"
phonon_voice = "Toby"
eleven_labs_voice_id = "h9CKu0pq1LSjIPhJEGCv"
```

Now names live in `names.toml` (male and female lists), and voices are
**voice profiles** that have a gender but no name:

```toml
[voice_groups.phonon.voices.voice_toby] # formerly Tarquin (ag_aca24036)
gender = "male"
phonon_voice = "Toby"
eleven_labs_voice_id = "h9CKu0pq1LSjIPhJEGCv"
```

When an agent first runs `context`, it draws the next name from a shuffled
**name bag**, then a voice of the same gender from a shuffled **voice bag**.
Every name is used once per round. When the bag runs out it refills, and the
next round's names get a number: `Alice2`, `Alice3`, and so on. Voices cycle
separately, so each profile is used once per cycle, and it's fine for several
agents to share one voice.

The generated name is now an agent's **only** identifier. The opaque `ag_…`
codes are gone; the `# formerly …` comments in `config.toml` record which old
identity used each voice.

### 2. Each agent keeps its own copy of its voice

When a name is assigned, the voice profile's settings (voice IDs, speeds,
approval flags, and absolute paths to voice files) are copied into the
assignment. Speech always uses that copy, never the live config. Editing,
moving, or deleting a profile only affects agents created afterwards. An
existing agent sounds exactly the same until its name is cleared.

### 3. One state file, one lock

All allocation state is in a single `allocation.json`: the reset epoch, the name
bag, the voice bags, and every pane or shell binding. Every writer takes the
same lock and replaces the file in one atomic step. Sends only read the file:
they never take a lock, create folders, or change permissions, so agents in a
sandbox can still send.

If the file is corrupt, from an unknown version, or still in the old format,
agent-comms stops and says to run `agent-comms bags reset --confirm`. It never
quietly starts over.

### 4. Epochs and explicit resets

`context` now returns an epoch with the name:

```json
{"id": "Dennis", "name": "Dennis", "epoch": "6a36ed123c6c8747", "prefix": ""}
```

`send` must include both `--name` and `--epoch`, and the listener checks them
against the caller's current binding before it accepts the message. The epoch
only changes on `agent-comms bags reset --confirm`. Reset:

- starts a new epoch at round 0 with full bags;
- clears every binding;
- keeps message history and voice approvals;
- backs up the old state, which also makes it the one-time migration from the
  old system.

`context --clear` and `--clear-all` still release names, but the names stay
used up for the rest of the round. `context --refill` was removed because a
manual refill could hand out a name that a live agent still had.

### 5. The conversation view

`agent-comms watch --view conversation` is a new, silent, full-pane view of who
is talking to whom:

```
 ◉ agent-comms conversation                    ● speaking  STATEMENT
  ╭───────╮                                          ╭───────╮
  │ (o_o) │  ◀──────────●━━━───────────────────────  │ (^_^) │
  ╰───────╯                                          ╰───────╯
  Oli                                                   Dennis
  ◆ human                                           ◆ dotfiles
  requester                                          responder
```

Senders can add optional display details. They only change what this view
shows; they never route or deliver a message:

- `--display-recipient NAME` (another agent, or `user`, which is the default)
- `--role requester|responder`
- `--kind instruction|query|statement`
- `--emotion neutral|happy|sad|concerned|curious|excited|frustrated`
- `--reply-to MESSAGE_ID`, to continue an earlier exchange (every send now
  prints its `Message ID:`)

The view shows each agent's tuios session and follows the same timeline as the
dock captions. It never plays audio, so you can open as many as you like.

### 6. History

- `history --watch [--refresh-time SECONDS]` keeps a live history view open. It
  redraws only when something changes and works while the listener is offline.
- New `--name` and `--epoch` filters; `--id` still works but is deprecated.
- Messages addressed to another agent show `Alice → Ben` in the heading.

### 7. Protocol and tooling

- The protocol version is now 3. The listener advertises its capabilities, and
  mixing old and new versions fails with "restart" guidance instead of strange
  errors.
- The listener publishes `start`, `display`, and `end` events for each message,
  which the caption and conversation views both follow.
- New `bags status` command, for checking the round, bag sizes, and current
  agents.
- `voice-review` now auditions voice profiles, not agents.
- The agent instructions, plugins, README, SPEC, voices skill, and tests (now 400)
  were all updated.

## Why it's better

- **No ceiling on agents.** The old system ran out at 24, and a new agent then
  had to wait or reclaim a pane that looked abandoned. Now there are 1,271 names
  and numbered rounds after that, so an agent never has to wait for a name.
- **Names and voices can change independently.** Adding names no longer means
  finding voices, and replacing a voice doesn't rename anyone. You can rebalance
  voices without touching who anyone is.
- **Matched genders.** Every agent's name and voice have the same gender, and the
  profiles state their gender explicitly instead of it being guessed.
- **Easier to follow.** `Dennis` in a message, pane title, history entry, or
  error is the same identifier everywhere. There's no `ag_…` code to look up.

## Why it's safer

- **Stale contexts can't send.** Names can repeat after a reset, so a name alone
  can't tell an old "Alice" from a new one. The epoch can: a context saved before
  a reset is rejected and the agent gets a fresh one.
- **Ownership comes from where the call actually runs.** The sender is still
  derived from the caller's real tuios pane or parent shell. The listener now
  re-checks that binding while it holds the lock, at the moment it accepts the
  message. `--name`, `--epoch`, and the display options can't select or
  impersonate another agent.
- **No duplicate names.** Allocation, clearing, and reset all go through one lock
  and one atomic write. Concurrent `context` calls for the same pane get the same
  answer, and different panes never get the same name. Names from cleared or
  pruned agents are never put back in the bag mid-round.
- **Fails closed.** Corrupt, old-format, or inconsistent state stops with clear
  recovery steps rather than quietly resetting. Reset writes the new state
  before deleting old files, so a crash can't leave nothing behind.
- **Recorded messages don't change.** Each message stores its sender's name,
  epoch, and voice settings when it's accepted, so later config edits, resets, or
  cleared agents don't rewrite what was already said.
- **Display details are only for display.** Recipients, roles, kinds, and
  emotions are validated and cleaned of terminal codes. They never route,
  deliver, or authorize anything.
- **Safe for sandboxes.** Sends still never write state, take locks, or change
  folder permissions.

## Why it's way cooler

- Agents get real, varied names, not just the same 24 regulars.
- The conversation view: framed ASCII faces that react to each message, a dot
  that travels between the speakers, speech bubbles that point at whoever is
  talking, session labels, kind chips, and a progress bar.
- Replies stay linked: `--reply-to` keeps both people on their own sides, so a
  back-and-forth reads like a conversation.
- A live history pane with `history --watch`.
- Agents can show an emotion when they speak. Nobody had a face before.

## Known limits

- The code doesn't check that a profile's ElevenLabs and Phonon voices are the
  same gender, so keep them matched when you edit profiles.
- Names added to `names.toml` start being used at the next automatic refill (or
  after `bags reset --confirm`). The current round still uses the old 505-name
  list.
- Leftover names in the current round are checked against the active voices when
  a name is drawn, not when the service starts.
- The view's events are `start`, `display`, and `end`; there is no separate
  cancel event yet.
