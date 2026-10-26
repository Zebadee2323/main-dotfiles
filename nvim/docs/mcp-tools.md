# Native mcp-tools integration

Tested upstream revision: `8806a2b6f8d1aaccbcf073c210bd870d810f5db3`.

## Deliberately small contract

Naia contributes individual tools to the existing upstream registry. It uses
upstream's args definitions, nvim_ names, plain text/JSON result formatting and
error handling. Walkthrough registrations use walkthrough_* (no ai_ prefix), so
harnesses see nvim_walkthrough_start, nvim_walkthrough_control, etc.

This replaces the earlier compatibility-heavy migration. The Python stdio
adapter, metadata catalog, result envelopes, name mapping, credential descriptor
and catalog poller have been removed. No upstream source changes are needed.

Some JSON Schema constraints are not advertised by upstream. Allowed values and
ranges should be described clearly and checked by handlers. Array item schemas
and object properties still carry useful nested structure; we do not encode
walkthrough steps or links as opaque JSON arguments. Portable tour files retain
their own strict schemas. Schema fidelity is not an authorization mechanism.

Register tools before connecting. Reconnect to discover additions/removals;
there are no custom list-change notifications. Normal walkthrough edits change
tour state and continue using the same registered tools.

## Connections and credentials

Harness -> authenticated mcp-tools HTTP endpoint -> Neovim RPC -> provider callback.

Sidekick still selects the exact editor through NVIM/NVIM_LISTEN_ADDRESS. It
queries connection_info() once for a current URL/token. That API is sensitive;
public status omits authentication. There is no secret file or observer timer.

The launcher injects NAIA_MCP_TOKEN into the harness environment. OpenCode's
remote config uses its env header substitution and disables OAuth. Claude's HTTP
config uses its environment header expansion. Codex uses bearer_token_env_var.
No bearer is embedded in command arguments or persisted configuration. Existing
same-name OpenCode/Codex server entries are replaced as a whole to avoid stale
stdio commands or credentials. Other servers/settings remain untouched.

Each connection has its own upstream MCP session. A bridge restart rotates
credentials and may change its ephemeral port: relaunch through Sidekick instead
of expecting an old connection to automatically adopt a fresh snapshot.

## Instructions

Sidekick launches OpenCode with a private V2 server and launch-only MCP/plugin
configuration. The `opencode/naia` plugin supplies instructions before every
agent-loop model call, so new sessions inside that TUI retain the voice/UI rules.
Ordinary OpenCode launches are not opted in. No new durable session instruction
entries are written; legacy entries and existing shared-service registrations
are not automatically removed. The private server still uses ordinary session
history/database persistence. Claude receives appended system instructions and
the reminder hook; Codex receives its instruction-file setting. The MCP server
itself does not supply Naia initialization instructions.

## Independent editor-safe lifecycle

The inspected upstream TypeScript SIGTERM/SIGINT handler calls nvim.quit(), which
the npm client implements as a force-quit of Neovim. Ollie's config has a separate
features/mcp_tools_runtime.lua compatibility guard. It has no Naia dependency:
it preserves upstream's API/settings, starts a direct Node/Bun child, and kills
only that child on stop. Direct Node uses tsx's import loader rather than npx/tsx
wrappers. The guard supplies independent MCPTools lifecycle commands and cleanup.

This guard uses upstream internals and remains version-sensitive. Removing the
adapter does not fix upstream shutdown. Retire the guard only after an upstream
fix and passing editor-survival tests.

## Remaining limitations

- The tested npm lockfile has 10 audit findings (4 moderate, 5 high, 1 critical).
  Review/update dependencies before production use; no automatic remediation here.
- Node needs 18.19+ and installed tsx development dependencies. Tests used Node;
  Bun's launch path is supported but not runtime-tested here.
- Callbacks remain synchronous on Neovim's event loop. Use upstream callback
  errors and harness execution timeouts; no custom cancellation/protocol layer.
- All enabled providers are visible. Harness approvals govern them; the launcher
  does not blanket auto-approve the whole shared server.
