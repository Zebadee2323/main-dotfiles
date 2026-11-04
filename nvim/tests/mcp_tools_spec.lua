-- Run: nvim --headless -u NONE -l tests/mcp_tools_spec.lua
local file = debug.getinfo(1, 'S').source:sub(2)
local root = vim.fn.fnamemodify(file, ':p:h:h')
local dependency = vim.env.MCP_TOOLS_PATH or (vim.fn.stdpath('data') .. '/lazy/mcp-tools.nvim')
vim.opt.rtp:prepend(root)
vim.opt.rtp:append(dependency)
vim.opt.rtp:append(vim.fn.stdpath('data') .. '/lazy/nvim-dap')
vim.opt.shadafile = 'NONE'
require('config.mcp_tools')
local mcp = require('mcp-tools')
local bridge = require('mcp-tools.bridge')
local config = require('mcp-tools.config')
assert(vim.wait(15000, function() return bridge.get_port() ~= nil end, 20), vim.inspect(bridge._stderr))
assert(not package.loaded.naia, 'The independently configured bridge must work without Naia')
assert(config.get('integrations.opencode') == false and config.get('integrations.ampcode') == false)
assert(config.get('bridge.tool_timeout') == 300000)
for _, category in ipairs({ 'dap', 'diagnostics', 'lsp' }) do
    assert(config.get('tools.' .. category) == true, category .. ' tools must be enabled')
end
for _, name in ipairs({ 'dap_status', 'diagnostics_list', 'lsp_hover', 'lsp_symbols' }) do
    assert(mcp.list_tools()[name], name .. ' must be registered')
end
local token = bridge.get_auth_token()
mcp.register({ name = 'owner_test', description = 'Ordinary shared tool', args = {}, execute = function(cb) cb('ok') end })
vim.cmd('MCPToolsStop')
assert(not mcp.is_running() and not bridge.get_auth_token())
assert(mcp.list_tools().owner_test, 'Stopping transport must not destroy provider registrations')
vim.cmd('MCPToolsStart')
assert(vim.wait(15000, function() return bridge.get_port() ~= nil end, 20), vim.inspect(bridge._stderr))
assert(bridge.get_auth_token() ~= token)
vim.cmd('MCPToolsRestart')
assert(vim.wait(15000, function() return bridge.get_port() ~= nil end, 20), vim.inspect(bridge._stderr))
require('features.mcp_tools_runtime').install() -- Idempotent configuration guard.
mcp.stop()
assert(not mcp.is_running())
print('PASS: independent mcp-tools configuration, disabled legacy integration, safe stop/restart')
