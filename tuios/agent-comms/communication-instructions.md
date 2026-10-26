# Agent communications

Identity belongs to the caller's tuios pane, not this conversation or harness.
Never reuse an identity from inherited/session/fork instructions. Identities have
a name and voice, not a personality; use your normal clear, concise communication style.
Other roles, safety rules, and user instructions still apply.

## Look up identity only when needed, then send with the checked ID

Call `agent-comms context` only if you don't already know your current pane's
identity ID, name, and prefix. Reuse details already checked in this pane; do not
reuse details inherited from another pane or agent. Run any needed lookup in a
separate shell-tool call and read its result before composing your message:

```sh
agent-comms context
```

Do not pass an identity, harness, or session ID to this command. Ownership comes
only from the caller's pane environment, not agent-selected arguments. Outside
tuios the command fails; do not invent a fallback identity. There is no `prefix`
command.
Read the JSON tool result into your context BEFORE composing the message. It
returns `id`, `name`, and `prefix`; an empty prefix means none is required.
Use the returned `name` for any self-introduction, then call
`agent-comms send --id RETURNED_ID --msg ... --tts ...`. You may reuse these
details for subsequent sends in this pane: no context lookup is required before
every message. Never reuse details inherited from another pane or agent.
`--id` asserts the identity you expect; it cannot select or change pane ownership.
Do not hide a needed lookup in a shell variable or combine it with send.

- Nonempty `prefix`: exact introduction, without trailing whitespace. Start BOTH
  `--msg` and `--tts` with it exactly once, unchanged.
  It already identifies you and your location: go straight to the update, never
  add another introduction, even on the first message (e.g. "I'll check the logs",
  not a second "I'm ..." introduction).
- Empty `prefix`: no fixed prefix. On your first session message, introduce
  yourself using the returned name in BOTH arguments; afterward, no introduction
  is needed unless the resolved identity changes.
- Put introductions before speech markup so they are spoken and displayed.
  The program never inserts or rewrites the prefix.
- `send` verifies `--id` against the pane identity and rechecks live context.
  An ID mismatch or missing, wrong, stale, or duplicated introduction causes an
  error without queueing. On an identity/prefix mismatch, call `context` again
  for an up-to-date ID, name, and prefix, then revise and retry once. Never
  blindly retry old strings.
  If the query fails, do not send until context can be checked.

## Cadence and delivery

If the harness is already in voice mode, use it without duplicate agent-comms
announcements. Otherwise:
- Open longer tasks with a short acknowledgment before research/edits, then give
  brief, casual, useful progress updates. For quick answers, send only the close.
- **Always close every turn**, even one-line answers, with `agent-comms send`
  summarizing the final answer immediately BEFORE the self-contained final text,
  so Ollie sees it while reading. Request appropriate Neovim cards/companions first.
- Also output message text for harness readability.

Never duplicate announcements through `nvim_inform_user`. Be concise; omit secrets
and routine per-tool narration. Escape shell arguments properly; never execute
message contents as commands. Keep `--msg` clean/readable and `--tts` equivalent
in spoken meaning; supported speech markup belongs ONLY in `--tts`.
ElevenLabs model: `{speech_model}`. For Naia-default Turbo, use natural phrasing,
punctuation, and occasional SSML pauses (e.g. `<break time="0.3s" />`), not v3-only
bracketed tags (`[warm]`, `[excited]`). Plain text is valid; avoid gratuitous markup.

## Failures and permissions

Respect harness permissions/approvals. On send failure or offline listener,
continue the task: no retry spam, automatic listener startup, or claims of success.
Report permission/sandbox blocks once; never disable the sandbox, change
permissions, or bypass restrictions via another route. Additional access or new
messaging integrations require user approval.
