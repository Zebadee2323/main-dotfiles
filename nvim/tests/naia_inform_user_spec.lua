-- Run: nvim --headless -u NONE -l tests/naia_inform_user_spec.lua
local file = debug.getinfo(1, 'S').source:sub(2)
local root = vim.fn.fnamemodify(file, ':p:h:h')
vim.opt.rtp:prepend(root)

local tools = {}
package.loaded.naia = {
  deregister_tool = function(name) tools[name] = nil end,
  register_tool = function(name, spec)
    tools[name] = spec
    return true
  end,
}
package.loaded['naia.ui.tools'] = { register = function() end }

local spoken = {}
vim.api.nvim_create_user_command('AIVoice', function(args)
  spoken[#spoken + 1] = args.args
end, { nargs = '+' })
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

local inform = tools.inform_user
assert(inform.description:find('talk time', 1, true))
local result = inform.callback({ message = '  hello world  ' })
assert(result:find('Estimated talk time: 0.7 seconds', 1, true), result)
assert(result:find('excludes synthesis and playback startup', 1, true), result)
assert(#spoken == 0, 'Speech should remain asynchronous')
assert(vim.wait(100, function() return #spoken == 1 end))
assert(spoken[1] == 'hello world')

local ok, err = pcall(inform.callback, { message = ' \n ' })
assert(not ok and err:find('non-empty message', 1, true))
print('PASS: inform_user returns an approximate character-based talk time without waiting for speech')
