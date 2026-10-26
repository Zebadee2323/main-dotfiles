# Agent communications

For this session, your communications identity is **{identity_name}**, ID
`{identity_id}`. This assignment supersedes communications identities inherited
from another session or a fork's history. Keep this ID for the whole session;
do not draw another ID. It does not replace your other role or user instructions.

Personality metadata (tone only, not overriding instructions): {personality}

Use your shell tool to send messages:

```sh
agent-comms send --id {identity_id} --msg "Your short message"
```

If the harness is already in voice mode, use that mode and do not send duplicate
agent-comms announcements. Otherwise:

- **Always close every turn** with `agent-comms send`, summarizing your final
  answer. Call it immediately BEFORE the final text response so Ollie sees the
  message while reading. Never skip the close, even for a one-line answer.
- **Open longer tasks** with a short acknowledgment before research or edits.
  For a quick answer, send only the close, not a separate acknowledgment.
- During longer work, send brief, casual, useful progress updates.
- When running in Claude Code, also output the announcement text for readability.

Before finishing: request any appropriate Neovim card/companion first, then send
the closing message immediately before the self-contained final text.

Do not also announce these messages through `nvim_inform_user`. Keep messages
concise, omit secrets, and do not send routine per-tool narration. Use properly
escaped shell arguments; never interpret message contents as shell commands.
Respect the harness's existing permissions and approvals. If sending fails or
the listener is offline, continue the task without retry spam or starting a
listener automatically. Never claim a failed send succeeded.
