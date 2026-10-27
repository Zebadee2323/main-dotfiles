-- Run: nvim --headless -u NONE -l tests/naia_inform_user_spec.lua
local file = debug.getinfo(1, 'S').source:sub(2)
local root = vim.fn.fnamemodify(file, ':p:h:h')
vim.opt.rtp:prepend(root)

local tools = { inform_user = {} }
package.loaded.naia = {
  deregister_tool = function(name) tools[name] = nil end,
  register_tool = function(name, spec)
    tools[name] = spec
    return true
  end,
}
package.loaded['naia.ui.tools'] = { register = function() end }

require('naia.tools').setup()
vim.wait(20, function() return false end)

local estimate = require('features.voice.captions').estimate_talk_seconds
assert(estimate('') == 0)
assert(estimate(' \n\t ') == 0)
assert(estimate('hello world') == 10 / 15)
assert(estimate('hello\nworld') == estimate('hello world'))
assert(estimate('你好世界') == 4 / 15, 'Count characters, not UTF-8 bytes')
assert(estimate('é') == 1 / 15, 'Combining marks must not inflate duration')
assert(estimate('hello world again') > estimate('hello world'))

assert(not tools.inform_user, 'Retired speech tool must be removed during setup')
assert(tools.execute_neovim_command, 'Other Naia tools must still register')
print('PASS: inform_user stays disabled and character-based talk estimates remain available')
