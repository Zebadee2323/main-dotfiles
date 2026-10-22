-- Run: nvim --headless -u NONE -l tests/mcp_tools_loader_spec.lua
local file = debug.getinfo(1, 'S').source:sub(2)
vim.opt.rtp:prepend(vim.fn.fnamemodify(file, ':p:h:h'))
local root = (vim.fn.has('win32') == 1 and 'C:' or '') .. '/MCP tools#%'
local bridge = {
    start = assert(loadstring('return function() end', '@' .. root .. '/lua/mcp-tools/bridge.lua'))(),
}
bridge.is_running = function() return bridge._process ~= nil end
package.loaded['mcp-tools.bridge'] = bridge
package.loaded['mcp-tools.config'] = { get = function() return nil end }
local command, env, on_exit
local process = { kill = function(_, signal)
    assert(signal == 'sigkill')
    on_exit({ code = 0 })
end }
vim.fn.executable = function(name) return name == 'node' and 1 or 0 end
vim.fn.filereadable = function() return 1 end
vim.system = function(cmd, opts, callback)
    command, env, on_exit = cmd, opts.env, callback
    return process
end
require('features.mcp_tools_runtime').install()
bridge.start({ nvim_socket = 'test-editor' })
local loader = root .. '/bridge/node_modules/tsx/dist/loader.mjs'
assert(command[1] == 'node' and command[2] == '--import')
assert(command[3]:match('^file:///'), 'Node loader must be a file URL, not a raw filesystem path')
assert(command[3] == vim.uri_from_fname(loader))
assert(command[3]:find('%%20') and command[3]:find('%%23') and command[3]:find('%%25'),
    'Loader URL must escape spaces, hashes and percent signs')
if vim.fn.has('win32') == 1 then assert(command[3]:match('^file:///C:'), 'Windows drive must be a file URL') end
assert(command[4] == root .. '/bridge/src/index.ts', 'Script entry point must stay a filesystem path')
assert(env.NVIM_LISTEN_ADDRESS == 'test-editor')
bridge.stop()
assert(not bridge.is_running())
print('PASS: Node loader uses an escaped file URL; entry point remains a path')
