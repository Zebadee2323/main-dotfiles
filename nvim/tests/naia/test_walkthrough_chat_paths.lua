local chat = require('naia.walkthrough.chat')
local paths = require('naia.walkthrough.paths')
vim.cmd('filetype plugin off') -- Isolated runtime has no Tree-sitter parsers.
assert(not package.loaded['naia.walkthrough.runtime'])
assert(_G.ai_walkthrough_foldtext == nil)
local original = vim.fn.getcwd()
local root = vim.fn.tempname()
vim.fn.mkdir(root .. '/source', 'p')
vim.fn.writefile({ 'first', 'second' }, root .. '/source/example.lua')
vim.cmd('cd ' .. vim.fn.fnameescape(root))
assert(paths.git_root(root) == nil, 'Failed git lookup must not return Git error text as a root')
assert(paths.resolve(' source/example.lua ') == paths.normalize(root .. '/source/example.lua'))
assert(paths.resolve('missing.lua') == nil)
assert(paths.resolve('') == nil and paths.resolve(false) == nil)
vim.cmd('edit ' .. vim.fn.fnameescape(root .. '/source/example.lua'))
vim.cmd('cd ' .. vim.fn.fnameescape(original))
assert(paths.resolve('example.lua') == paths.normalize(root .. '/source/example.lua'), 'Resolve against source directory')

local buffer = vim.api.nvim_create_buf(false, true)
local win = vim.api.nvim_get_current_win()
vim.api.nvim_win_set_buf(win, buffer)
vim.bo[buffer].modifiable = false
local followed, opened, paused = 0, 0, 0
local target = { bufnr = buffer, hidden = false }
target.ui = {
  winnr = win,
  is_visible = function() return true end,
  open = function() opened = opened + 1 end,
  follow = function() followed = followed + 1 end,
  unlock_buf = function() vim.bo[buffer].modifiable = true end,
  lock_buf = function() vim.bo[buffer].modifiable = false end,
}
package.loaded.codecompanion = { buf_get_chat = function() return target end }
local state = { active = true, index = 1, walkthrough_id = 'test-tour', revision = 7, steps = {
  { id = 12, path = 'example.lua', line_start = 1, line_end = 2, description = 'Explain this' },
} }
local function pause() paused = paused + 1 end
assert(chat.instruct(state, pause, { args = '  Why?  ' }))
local text = table.concat(vim.api.nvim_buf_get_lines(buffer, 0, -1, false), '\n')
assert(text:find('walkthrough_id: test-tour', 1, true) and text:find('expected_revision: 7', 1, true))
assert(text:find('nvim_walkthrough_insert_steps', 1, true) and text:find('User instruction:\nWhy?', 1, true))
assert(not vim.bo[buffer].modifiable and followed == 1 and paused == 1)
assert(vim.wo[win].foldtext == "v:lua.require'naia.walkthrough.chat'.foldtext()")
assert(vim.fn.foldtextresult(1) == 'User prompt for walkthrough:', 'Fold labels must resolve through a module, not a global')
local lines = vim.api.nvim_buf_line_count(buffer)
target.current_request = true
assert(chat.instruct(state, pause, { args = 'Busy' }) == false)
assert(vim.api.nvim_buf_line_count(buffer) == lines)
target.current_request = nil
target.hidden = true
assert(chat.instruct(state, pause, { args = 'Hidden' }) == nil)
assert(vim.api.nvim_buf_line_count(buffer) == lines and opened == 0)
vim.cmd('enew!')
vim.fn.delete(root, 'rf')
print('PASS: optional chat instruction/fold integration and live source path resolution')
