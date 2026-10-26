# Neovim configuration

- `init.lua` loads `lua/core/` and bootstraps Lazy.
- `lua/core/` contains editor options, general mappings, commands, and Neovide
  settings. `core/init.lua` also enables the early scrolling and terminal helpers.
- `lua/config/lazy.lua` bootstraps Lazy and imports `lua/plugins/*.lua`.
- `lua/plugins/` contains plugin specs grouped by plugin or feature. Declare
  dependencies, build steps, loading triggers, and setup callbacks here.
- `lua/config/` contains the larger setup modules, loaded only by the owning
  spec's `config` callback. Small setups can use `opts` directly in the spec.
- `lua/features/` contains custom functionality, grouped by feature rather than
  scattered among runtime scripts. `voice/` and `file_change_fx/`
  own their related modules; small utilities stay as single modules.
- `after/plugin/features.lua` is the sole custom-feature runtime hook. It calls
  `features.setup()` after plugin loading, preserving the existing startup phase
  and ordering for voice, file-change effects, and standalone utilities.
- `after/ftplugin/` contains filetype-local wrapping and Git folding settings,
  including undo instructions for filetype changes.

### Custom-feature ownership

`features.voice.setup()` and `features.file_change_fx.setup()` load their runtime
modules once. Those runtimes retain ownership of state, commands, timers, jobs,
watchers, and shutdown cleanup. The initial layout migration deliberately keeps
their implementation intact; splitting providers, rendering, or watching into
smaller modules can happen separately without mixing behaviour changes into moves.
Small standalone helpers register commands on their first `require`.

Naia is a first-party subsystem in `lua/naia/`, not a local Lazy plugin. The
MCP dependency's setup callback loads `config/naia.lua` after configuring the
shared bridge; Sidekick and CodeCompanion depend on that same MCP plugin.
There is no runtime dependency on `~/Development/nvim-naia`.

- `naia/init.lua`, `backend.lua`, `state.lua`, `config.lua`: provider API,
  registration ownership and connection information (not transport lifecycle).
- `naia/commands.lua`: explicit, idempotent setup for `:Naia*` commands.
- `naia/tools.lua`: voice notifications and permission-gated Ex commands.
- `naia/ui/`: companion, cards, approval policy, actions, styling and MCP tools.
- `naia/walkthrough/`: playback, tool/edit interface, portable files, layout and
  confirmation prompts. Legacy chat/YAML response parsing is isolated in
  `response.lua`. `require('naia.walkthrough').setup()` is unchanged.
- `bin/sidekick-naia-cli` and its instruction files: launch-scoped harness setup.
- `opencode/naia/`: the Sidekick-only OpenCode instruction plugin.

Voice playback remains in `features/voice/`; the shared MCP safety guard stays
independent in `features/mcp_tools_runtime.lua`. UI commands and cleanup hooks
are registered when the UI module is first required. Tools register once during
setup, not again on a scheduled retry. Existing MCP names, walkthrough revision
guards, approval policy and public `require('naia')` API are preserved.

Run `make check` from this directory for configuration specs plus the imported
provider, launcher, instruction-plugin and walkthrough regressions. Imported tests
live in `tests/naia/`, without the standalone project's historical phase folders.
The config specs using the real bridge registry require the installed MCP plugin.
`make test-integration` exercises live HTTP transport; `make
test-opencode-integration` exercises an installed OpenCode against a fake model.
Both integration suites are opt-in. See [walkthrough reference](docs/walkthrough.md)
and [bridge notes](docs/mcp-tools.md) for details.

The launcher requires Python 3.9+; the bridge requires Bun or Node.js 18.19+
with its npm dependencies installed. Walkthroughs require Neovim 0.11+.
After this migration, restart Neovim and relaunch Sidekick rather than hot-reloading
the existing bridge/session. The old standalone repository is not modified or
deleted, but is no longer loaded by this configuration.

Assets have not moved: voice models remain in `after/ai_voices/`, and the effect
sound remains at `after/plugin/change-fx.mp3`. Their paths are independent of the
Lua module layout. Machine-local voice preferences remain outside this repository.

Plugins remain eagerly loaded to preserve the existing startup behavior. To
defer a plugin, add `lazy = true` and appropriate `keys`, `cmd`, `event`, or `ft`
triggers to its spec. Keep its startup mappings in Lazy's `keys` field so they
can trigger loading; mappings defined only in `config` will not exist yet.
Check dependencies and other modules that require the plugin as well: they can
cause it to load earlier than its own triggers.

Use `:Lazy profile` to inspect loading costs. Plugin versions remain pinned in
`lazy-lock.json`; this layout change does not require updating them.

## Naia companion and editor tools

### Shared MCP bridge ownership

`guill/mcp-tools.nvim` is independently configured by `config/mcp_tools.lua` and
starts before Naia loads. Naia registers individual tools alongside other
providers; it does not initialize, configure, start or stop this shared server.
The upstream OpenCode integration stays disabled because Sidekick handles the
OpenCode V2 connection to the exact parent editor.

`features/mcp_tools_runtime.lua` is a temporary, Naia-independent safety guard:
the pinned upstream shutdown handler sends `qa!` to Neovim. The guard retains
the normal mcp-tools public API/settings but runs a direct Node/Bun child and
terminates only that child on stop. It relies on upstream bridge internals and
should be revisited when upstream fixes shutdown. The dependency's npm security
advisories also remain unresolved; review package updates before production use.
The Node import loader is passed as an escaped file URL, including on Windows;
the script entry point remains a normal filesystem path.

- `:MCPToolsStart`, `:MCPToolsStop`, `:MCPToolsRestart`, `:MCPToolsStatus` control
  the shared transport independently of Naia.
- `:NaiaStart`, `:NaiaStop`, `:NaiaRestart` attach/detach only Naia registrations.
- Sidekick configures native HTTP connections to the existing bridge; no stdio
  adapter is used and it never starts transport. Relaunch through Sidekick after
  a bridge restart to refresh its launch-scoped URL/token. Claude/Codex approvals follow the
  harness's policy rather than blanket approval of every shared-server tool.
- Run `nvim --headless -u NONE -l tests/mcp_tools_spec.lua` to test configuration
  and editor-safe transport stop/restart without loading Naia.
- Tool definitions use upstream `args`, not full input schemas. Responses are
  ordinary text/JSON; all MCP tool names have upstream's `nvim_` prefix.
  Walkthrough tools are `nvim_walkthrough_*` (no `ai_`). Register before connecting;
  reconnect after adding/removing tools. Existing walkthrough edits need no reconnect.
- Instructions are supplied through Sidekick, not the MCP server. Its private
  OpenCode server injects them into every session, including new in-TUI sessions.
  Claude and Codex still receive them at launch time.
- Run `nvim --headless -u NONE -l tests/naia_native_tools_spec.lua` to verify your
  configured tools against the upstream registry without starting a bridge.

Naia exposes `nvim_show_companion`, `nvim_show_card`, `nvim_set_quickfix`, and
`nvim_jump_to_location` through MCP. Requests return an ID immediately rather than
blocking Neovim. `nvim_get_ui_request_status` reports the actual outcome; pending
does not mean an action has happened. Approval requests expire after two minutes.

All four UI policies—companion, card, quickfix and navigation—**auto-allow by
default**. Explicit startup/session policies can still require approval or
disable a capability. Auto-allowed requests are queued non-blockingly; pending
does not yet mean applied. The small pinned card opens without taking focus and
is limited to eight wrapped rows at 46 columns. It does not replace the voice panel.
These defaults do not change harness tool permissions or walkthrough confirmation.

- Approval prompts sit at the bottom centre; pinned cards remain in the top-right.
- Passive approvals show a compact summary with action, target, reason, and plain
  keybinding/command hints—no buttons or button-navigation mappings.
  Use `:NaiaApprove` or `:NaiaReject` directly. There is no separate review step.
  Approval actions are keyboard-only; no mouse handlers are installed.
- Cards, approvals, and the voice visualiser use opaque, contrasting theme-derived
  surfaces. Cards use the informational accent; approvals use the warning accent. Colours follow
  theme changes without changing the background of regular editing buffers.
- Bottom-anchored voice visualisers automatically sit above short, wide bottom
  splits overlapping their horizontal position, including stacked panels. Top
  anchors are unchanged; configured vertical offsets apply above the panels.
- `:NaiaApprove [id]` / `:NaiaReject [id]` act on the displayed request by default.
- In the approval card: `y` allows once, `n`/`q` reject, `a` allows this capability
  for the session, and `m` disables it for the session. These keys apply only when
  the prompt is focused; commands work directly from the editor.
- `:NaiaUIPolicy {companion|quickfix|navigation|card} {ask|allow|off}` controls each
  feature independently. `off` silently rejects future requests and cancels
  pending ones; for cards it also closes the current float. `allow` skips future
  prompts. Policies are session-local and are never changed by tool calls.

For persistent defaults, set this before Naia loads (for example, in `init.lua`):

```lua
vim.g.naia_ui_policies = {
  companion = "ask", -- prompt
  quickfix = "ask",
  navigation = "off", -- auto-reject silently
  card = "allow", -- auto-approve
}
```

Omitted features remain auto-allowed. Change a feature immediately for the
current session with `:NaiaUIPolicy companion allow` (auto-approve),
`:NaiaUIPolicy companion off` (auto-reject), or `:NaiaUIPolicy companion ask`.
Setting `off` also cancels that feature's pending approvals. Configuration is
user-owned; agents must not change it to bypass approvals.

### Companion and card controls

- `:NaiaCompanion` opens/focuses the companion. `q` closes its view; `]l`/`[l`
  move between source links and Enter requests navigation. Ctrl-O jumps back.
- Every approved companion creates a new named, listed buffer; earlier content
  and its links remain intact until you delete those buffers. The companion
  window is reused rather than accumulating splits. Use `:NaiaCompanionPrevious`
  / `:NaiaCompanionNext`, or `[h` / `]h` inside a companion, to cycle history.
  Companion buffers stay read-only, but the split is not buffer-locked: Telescope
  or ordinary buffer commands may replace it. The snapshot remains in history;
  reopening notes will not overwrite a window you have repurposed for a file.
  Normal buffer pickers and `:buffers` also show these snapshots.
- `:NaiaCard` toggles the pinned float: close it if showing, otherwise reopen/focus
  the selected snapshot. `q` or `:NaiaCardClose` explicitly dismisses it.
  Dismissing a card does not disable later cards; use `:NaiaUIPolicy card off` for that.
- Cards keep the last **eight** snapshots in memory. `:NaiaCardPrevious` /
  `:NaiaCardNext` cycle without taking focus; `[h` / `]h` work inside a focused
  card. `:NaiaCard` reopens the selected snapshot after dismissal. History is
  session-local; muting stops agent updates but does not prevent manual browsing.
- Cards share the voice panel's dark background. New cards settle for 100ms, then
  pulse three times (150ms each, with 100ms gaps); history browsing does not flash. Disable with
  `vim.g.naia_ui_card_flash = false`.

Companion text is ordinary Markdown. Pair inline source links with structured
targets so paths and locations never have to be interpreted as editor commands:

```json
{
  "title": "Speech playback",
  "content": "See the [speech queue](lua/features/voice/runtime.lua#L696).",
  "links": [{
    "display_line": 1,
    "label": "speech queue",
    "path": "lua/features/voice/runtime.lua",
    "line": 696
  }],
  "reason": "Explain how queued speech reaches playback"
}
```

Paths resolve against Neovim's working directory at request time. Omitting
`display_line` appends a link in a Sources section. Only supplied source targets
are navigable; arbitrary Markdown URLs are not opened or executed. Link jumps
obey the same navigation policy as agent-requested jumps, independently of the
companion/card policy.

Quickfix defaults to a new list, retaining the previous one in history.
Append/replace requests refuse to apply if the current list has changed while
approval was pending. Quickfix opens only when requested and never auto-jumps.
The existing generic `execute_neovim_command` tool retains its separate permission
system; these new tools do not grant it additional privileges.

Run the isolated UI regression tests with:

```sh
nvim --headless -u NONE -l tests/naia_ui_spec.lua
nvim --headless -u NONE -l tests/naia_ui_policy_spec.lua
nvim --headless -u NONE -l tests/naia_ui_defaults_spec.lua
nvim --headless -u NONE -l tests/mcp_tools_loader_spec.lua
nvim --headless -u NONE -l tests/layout_spec.lua
```
