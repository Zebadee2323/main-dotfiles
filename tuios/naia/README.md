# tuios → Neovim Naia shims

Launch `opencode`, `codex`, or `claude` in a tuios pane and connect it to a ready
`nvim-naia` editor in the **same session and workspace**.

## Use

Put this repository's `bin` directory before the real CLIs in your PATH:

```sh
export PATH="$HOME/.config/dotfiles/tuios/naia/bin:$PATH"
opencode
codex
claude
```

Or invoke `./bin/opencode`, `./bin/codex`, or `./bin/claude` directly.
All arguments are forwarded.
No shell configuration is changed automatically. Keep the scripts together in
`bin`; use PATH rather than copying or symlinking individual scripts.

Requirements: Python 3, `tuios`, `nvim`, `ps`, `lsof`, the real target CLI, and
an editor with `nvim-naia` configured and ready (`:NaiaStatus`). macOS and Linux
process/socket discovery is supported; remote-host editors and TCP-only Neovim
RPC listeners are not discovered.

Discovery uses the launching pane's workspace, even if the client is viewing
another one. It finds Neovim descendants of that workspace's pane processes,
including forked headless RPC servers, then asks their Unix sockets for readiness
and `require('naia').wrapper_path()`. It never starts an editor or bridge, types
into a pane, or scans unrelated editor sockets.

The selected editor's **existing Sidekick launcher** handles MCP registration,
authentication and agent instructions. OpenCode uses a private V2 server and
its Naia instruction plugin; Codex and Claude receive launch-scoped MCP/instruction
overrides. Tokens stay in the launch environment, not arguments or files.
Existing harness permissions remain in force. No global/project config is written.
OpenCode administrative commands are handled by the upstream launcher without
Naia config; explicit `--server` conversation launches are rejected upstream.

Outside tuios these shims launch the real CLI unchanged. Inside tuios, inherited
`NVIM`, `NVIM_LISTEN_ADDRESS`, and `NAIA_MCP_TOKEN` are cleared before discovery.
No ready editor means a warning and an ordinary launch. Discovery errors and
ambiguous editor matches cancel the launch instead of guessing.

Select a particular editor pane when multiple editors are ready:

```sh
tuios list-windows --json
TUIOS_NAIA_PANE=65eb4e59 opencode
```

The selector is a unique full/prefix pane ID in the caller's workspace. Multiple
ready editors within that one pane remain ambiguous. Relaunch after restarting
the editor's MCP bridge to refresh its URL/token.

## Tests

```sh
python3 -m unittest discover -s tests -v
```
