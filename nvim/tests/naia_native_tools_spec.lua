-- Run: nvim --headless -u NONE -l tests/naia_native_tools_spec.lua
local file = debug.getinfo(1, 'S').source:sub(2)
local root = vim.fn.fnamemodify(file, ':p:h:h')
vim.opt.rtp:prepend(root)
vim.opt.rtp:append(vim.fn.stdpath('data') .. '/lazy/mcp-tools.nvim')
local naia_path
for _, spec in ipairs(dofile(root .. '/lua/plugins/ai.lua')) do
    if type(spec) == 'table' and spec.name == 'nvim-naia' then naia_path = spec.dir end
end
vim.opt.rtp:append(assert(naia_path, 'Missing local Naia plugin spec'))
vim.g.naia_ui_policies = { card = 'off', companion = 'off', navigation = 'off', quickfix = 'off' }
local mcp = require('mcp-tools')
mcp.setup()
local _, err = require('naia').setup()
assert(not err, err)
require('features.naia_tools').setup()
vim.wait(20, function() return false end)
local tools = mcp.list_tools()
for _, name in ipairs({ 'inform_user', 'execute_neovim_command', 'show_card', 'show_companion',
    'set_quickfix', 'jump_to_location', 'get_ui_request_status', 'get_working_directory' }) do
    assert(tools[name], name .. ' did not register using upstream args')
end
assert(tools.inform_user.args.message.required and tools.show_card.args.title.required)
local registry = require('mcp-tools.registry')
local muted = registry.execute('show_card', { title = 'Native test', content = 'Text result', reason = 'Test policy' })
assert(muted.done and not muted.error and type(muted.result) == 'string')
assert(vim.json.decode(muted.result).status == 'muted', 'Native calls must respect the same UI policy')
local missing = registry.execute('show_card', { content = 'Missing required title', reason = 'Test validation' })
assert(missing.error and missing.error:find('required argument', 1, true))
assert(not mcp.is_running(), 'Tool registration must never start transport')
print('PASS: configured Naia tools use native upstream args, text results and unchanged UI policies')
