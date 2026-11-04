-- Run: nvim --headless -u NONE -l tests/naia_ui_defaults_spec.lua
local file = debug.getinfo(1, 'S').source:sub(2)
vim.opt.rtp:prepend(vim.fn.fnamemodify(file, ':p:h:h'))
assert(vim.g.naia_ui_policies == nil, 'Default-policy test must not configure overrides')
vim.g.naia_ui_card_flash = false
local ui = require('naia.ui')
local target = vim.fn.tempname() .. '.txt'
vim.fn.writefile({ 'target line' }, target)
local requests = {
    companion = { title = 'Default companion', content = 'Notes', reason = 'Test default policy' },
    card = { title = 'Default card', content = 'Note', reason = 'Test default policy' },
    quickfix = { title = 'Default quickfix', items = { { path = target, text = 'Finding' } }, reason = 'Test default policy' },
    navigation = { path = target, line = 1, reason = 'Test default policy' },
}
for _, kind in ipairs({ 'companion', 'card', 'quickfix', 'navigation' }) do
    local request = ui.request(kind, requests[kind])
    assert(request.message:find('Allowed by session policy', 1, true), kind .. ' did not default to auto-allow')
    assert(vim.wait(1000, function() return ui.status(request.request_id).status ~= 'pending' end, 10))
    assert(ui.status(request.request_id).status == 'applied', kind .. ' failed to auto-apply')
end
for _, window in ipairs(vim.api.nvim_list_wins()) do
    assert(not vim.w[window].naia_approval, 'Auto-allow defaults must not open an approval prompt')
end
assert(vim.g.naia_ui_policies == nil, 'Runtime defaults must not write user startup configuration')
ui.set_policy('companion', 'ask')
local pending = ui.request('companion', requests.companion)
vim.wait(30, function() return false end, 5)
assert(ui.status(pending.request_id).status == 'pending', 'Explicit ask must still wait for approval')
ui.reject(pending.request_id)
ui.set_policy('card', 'off')
assert(ui.request('card', {}).status == 'muted', 'Explicit off must still disable the UI capability')
vim.fn.delete(target)
print('PASS: all UI policies auto-allow by default; explicit ask/off overrides remain effective')
