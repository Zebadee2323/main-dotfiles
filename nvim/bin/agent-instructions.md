The user is called Ollie.

# Tool names and connection

Use the tools from the `naia` MCP server. They use upstream's `nvim_` prefix:
`nvim_show_card`, `nvim_show_companion`, `nvim_set_quickfix`,
`nvim_jump_to_location`, and `nvim_get_ui_request_status`. Discover tools by name
when needed. Walkthrough tools are `nvim_walkthrough_*`, without an `ai_` prefix.
Other enabled mcp-tools providers share this server and follow harness permissions.

Tool results are plain text or JSON encoded as text. There is no adapter or native
rich-content contract. Reconnect after changing the registered tool set. Relaunch
through Sidekick after the bridge restarts. Sidekick-launched OpenCode uses a
private server whose Naia plugin supplies these rules to every session, including
new sessions created inside the TUI. Other harnesses still require launch-time
preparation; do not assume their new sessions inherit these rules.

# Showing things in Neovim

Use the dedicated tools rather than generic editor commands:

- `nvim_show_card`: commands, keybindings, reusable snippets and tiny notes, within
  eight wrapped rows at 46 columns. Even a one-line command answer deserves a card.
- `nvim_show_companion`: longer explanations, comparisons and evidence. Provide
  structured source links (`label`, `path`, `line`, optional `display_line`).
- `nvim_set_quickfix`: actionable findings or multiple locations. Prefer a new list,
  label informational bookmarks and never auto-jump.
- `nvim_jump_to_location`: explicitly requested navigation, or when opening a
  location materially helps. Otherwise let Ollie choose companion links.

Skip displays for acknowledgments, purely conversational replies, redundant
progress updates, explicit chat-only requests, or unavailable tools.

Respect approvals. Pending is NOT success. Read `nvim_get_ui_request_status` only
on demand, never in a polling loop. Never retry rejected/muted requests, approve
on Ollie's behalf, change policies, or bypass them through another tool.

When asked for a walkthrough, use `nvim_walkthrough_start` or
`nvim_walkthrough_load`. Keep confirmation enabled unless Ollie explicitly asks
to skip it. A pending tour is not active. Fetch `nvim_walkthrough_current_context`
before mutations and preserve its revision/playback guards. Use
`nvim_walkthrough_file_spec` before authoring a portable tour file.

Communications identity and messaging guidance are supplied separately by the
agent-comms harness integration. Keep the Neovim display guidance above.
