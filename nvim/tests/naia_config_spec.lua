-- Run: nvim --headless -u NONE -l tests/naia_config_spec.lua
local root = vim.fn.fnamemodify(debug.getinfo(1, 'S').source:sub(2), ':p:h:h')
vim.opt.rtp:prepend(root)

local calls = {}
for _, name in ipairs({ 'naia', 'naia.commands', 'naia.tools', 'naia.walkthrough' }) do
    package.loaded[name] = { setup = function() calls[#calls + 1] = name end }
end
package.loaded['config.mcp_tools'] = true
local specs = dofile(root .. '/lua/plugins/ai.lua')
local bridge
for _, spec in ipairs(specs) do
    if type(spec) == 'table' then
        assert(not spec.dir and spec.name ~= 'nvim-naia', 'Naia must not be a local plugin')
        if spec[1] == 'guill/mcp-tools.nvim' then bridge = spec end
        if spec[1] == 'folke/sidekick.nvim' or spec[1] == 'olimorris/codecompanion.nvim' then
            assert(vim.tbl_contains(spec.dependencies, 'guill/mcp-tools.nvim'))
        end
    end
end
assert(bridge, 'Missing shared MCP dependency')
bridge.config()
assert(vim.deep_equal(calls, { 'naia', 'naia.commands', 'naia.tools', 'naia.walkthrough' }),
    'Provider, commands, editor tools and walkthrough must initialize in order')

package.loaded['naia.commands'] = nil
local commands = require('naia.commands')
assert(vim.fn.exists(':NaiaStatus') == 0, 'Requiring commands must not initialize them')
commands.setup()
local first = vim.api.nvim_get_commands({}).NaiaStatus
commands.setup()
assert(vim.deep_equal(first, vim.api.nvim_get_commands({}).NaiaStatus))
for _, name in ipairs({ 'NaiaStart', 'NaiaStop', 'NaiaRestart', 'NaiaStatus' }) do
    assert(vim.fn.exists(':' .. name) == 2, 'Missing command: ' .. name)
end
package.loaded.naia = nil
local wrapper = require('naia').wrapper_path()
assert(wrapper == root .. '/bin/sidekick-naia-cli', 'Launcher must resolve inside this config')
assert(vim.fn.executable(wrapper) == 1, 'Launcher must stay executable')
print('PASS: first-party Naia startup, dependency ordering, commands and launcher path')
