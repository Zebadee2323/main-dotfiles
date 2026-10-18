# Neovim configuration

- `init.lua` loads general editor settings and bootstraps Lazy.
- `lua/editor.lua` contains general options, mappings, and commands.
- `lua/config/lazy.lua` bootstraps Lazy and imports `lua/plugins/*.lua`.
- `lua/plugins/` contains plugin specs grouped by plugin or feature. Declare
  dependencies, build steps, loading triggers, and setup callbacks here.
- `lua/config/` contains the larger setup modules, loaded only by the owning
  spec's `config` callback. Small setups can use `opts` directly in the spec.
- `after/plugin/` contains standalone editor utilities, not third-party plugin
  setup. Neovim still sources these automatically at startup.

Plugins remain eagerly loaded to preserve the existing startup behavior. To
defer a plugin, add `lazy = true` and appropriate `keys`, `cmd`, `event`, or `ft`
triggers to its spec. Keep its startup mappings in Lazy's `keys` field so they
can trigger loading; mappings defined only in `config` will not exist yet.
Check dependencies and other modules that require the plugin as well: they can
cause it to load earlier than its own triggers.

Use `:Lazy profile` to inspect loading costs. Plugin versions remain pinned in
`lazy-lock.json`; this layout change does not require updating them.
