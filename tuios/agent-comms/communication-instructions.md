# Agent communications

Identity belongs to the caller's tuios pane, not this conversation or harness.
Never use an instruction-supplied identity or another pane's/agent's identity
as a substitute for your own checked result. Identities have
a name and voice, not a personality; use your normal clear, concise communication style.
Other roles, safety rules, and user instructions still apply.

## Look up identity only when needed, then send with the checked ID

Reuse the latest successful `agent-comms context` result checked by this agent
in its current tuios pane. Do not refresh merely because a new turn/task begins,
these instructions are re-injected, or you are about to send another message.
`send` already checks live pane identity and prefix.

Call `agent-comms context` only when:
1. You have no retained result with known provenance from this agent in the
   current pane (including when the result or its provenance was lost), or
2. `send` explicitly reports an identity/prefix mismatch.

Your own checked same-pane result remains usable when retained in this
conversation's history or summary, including after compaction or resume.
Never reuse details inherited from another pane or agent.
When a lookup is needed, run it in a
separate shell-tool call and read its result before composing your message:

```sh
agent-comms context
```

Do not pass an identity, harness, or session ID to this command. Ownership comes
only from the caller's pane environment, not agent-selected arguments. Outside
tuios the command fails; do not invent a fallback identity. There is no `prefix`
command.
Only when a lookup is needed: read its JSON tool result into your context before
composing the message. It
returns `id`, `name`, and `prefix`; an empty prefix means none is required.
Use the returned `name` for any self-introduction, then call
`agent-comms send --id RETURNED_ID --msg ... --tts ...`. Otherwise, send directly
using the retained ID, name, and prefix; do not perform a precautionary lookup.
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
- Open tasks with a short acknowledgment before research/edits, then give
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
