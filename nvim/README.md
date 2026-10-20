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
  scattered among runtime scripts. `voice/`, `naia_ui/`, and `file_change_fx/`
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

Naia-dependent setup belongs to `config/naia.lua`, called by Naia's Lazy spec:
custom MCP tools live in `features/naia_tools.lua`, UI modules in
`features/naia_ui/`, and the walkthrough is provided by the external Naia plugin.
The `naia` module namespace remains reserved for that plugin. UI commands and
cleanup hooks are registered when the UI module is first required.

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

Naia exposes `show_companion`, `show_card`, `set_quickfix`, and
`jump_to_location` through MCP. Requests return an ID immediately rather than
blocking Neovim. `get_ui_request_status` reports the actual outcome; pending
does not mean an action has happened. Approval requests expire after two minutes.

Companion splits, quickfix updates, and navigation require approval by default.
The small pinned card is allowed by default, opens without taking focus, and is
limited to eight wrapped rows at 46 columns. It does not replace the voice panel.

- Approval prompts sit at the bottom centre; pinned cards remain in the top-right.
- Passive approvals show a compact summary with action, target, reason, and plain
  keybinding/command hints—no buttons or button-navigation mappings.
  Use `:NaiaApprove` or `:NaiaReject` directly. There is no separate review step.
  Approval actions are keyboard-only; no mouse handlers are installed.
- Cards, approvals, and the voice visualiser use opaque, contrasting theme-derived
  surfaces. Cards use the informational accent; approvals use the warning accent. Colours follow
  theme changes without changing the background of regular editing buffers.
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

Omitted features retain their defaults. Change a feature immediately for the
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
nvim --headless -u NONE -l tests/layout_spec.lua
```
