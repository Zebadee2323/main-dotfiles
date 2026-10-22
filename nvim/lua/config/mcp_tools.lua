-- MCP tools is an independent service. Naia is only one tool provider.
-- Keep the legacy opencode.nvim integration disabled: Sidekick configures V2.
require('features.mcp_tools_runtime').install()
local mcp = require('mcp-tools')
mcp.setup({
    tools = {
        dap = true,         -- Debug Adapter Protocol tools
        diagnostics = true, -- LSP diagnostics
        lsp = true,         -- LSP hover and symbols
    },
    integrations = { opencode = false, ampcode = false },
})
mcp.start()
