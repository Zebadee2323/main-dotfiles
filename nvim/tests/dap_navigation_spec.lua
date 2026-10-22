-- Run: nvim --headless -u NONE -l tests/dap_navigation_spec.lua
local file = debug.getinfo(1, "S").source:sub(2)
vim.opt.rtp:prepend(vim.fn.fnamemodify(file, ":p:h:h"))
local navigate = require("config.dap-navigation").switchbuf
local api = vim.api
local source = api.nvim_get_current_win()
local original = api.nvim_get_current_buf()
local target = api.nvim_create_buf(true, false)
api.nvim_buf_set_lines(target, 0, -1, false, { "first", "second" })

-- Reuse an unlocked editor even when a locked utility window has focus.
vim.cmd("vsplit")
local utility = api.nvim_get_current_win()
local utility_buf = api.nvim_create_buf(false, true)
api.nvim_win_set_buf(utility, utility_buf)
vim.wo[utility].winfixbuf = true
navigate(target, 2, 2)
assert(api.nvim_get_current_win() == source)
assert(api.nvim_win_get_buf(source) == target)
assert(api.nvim_win_get_buf(utility) == utility_buf and vim.wo[utility].winfixbuf)
assert(vim.deep_equal(api.nvim_win_get_cursor(source), { 2, 1 }))
assert(#api.nvim_tabpage_list_wins(0) == 2)

-- An already-displayed target can be navigated without unlocking its window.
vim.wo[source].winfixbuf = true
api.nvim_set_current_win(utility)
navigate(target, 1, 1)
assert(api.nvim_get_current_win() == source and vim.wo[source].winfixbuf)
assert(#api.nvim_tabpage_list_wins(0) == 2)

-- When all existing windows are protected, create only one reusable split.
api.nvim_set_current_win(utility)
navigate(original, 1, 1)
local created = api.nvim_get_current_win()
assert(created ~= source and created ~= utility and not vim.wo[created].winfixbuf)
assert(vim.wo[source].winfixbuf and vim.wo[utility].winfixbuf)
assert(#api.nvim_tabpage_list_wins(0) == 3)
local next_buf = api.nvim_create_buf(true, false)
navigate(next_buf, 999, 999)
assert(api.nvim_get_current_win() == created)
assert(#api.nvim_tabpage_list_wins(0) == 3)

-- Preserve DAP REPL focus while still updating the source window.
vim.bo[utility_buf].filetype = "dap-repl"
api.nvim_set_current_win(utility)
navigate(original, 1, 1)
assert(api.nvim_get_current_win() == utility)
assert(api.nvim_win_get_buf(created) == original)
print("PASS: DAP source navigation respects locked windows and reuses editor splits")
