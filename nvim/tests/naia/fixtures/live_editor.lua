local dependency = assert(vim.env.MCP_TOOLS_PATH)
vim.opt.runtimepath:append(dependency)
package.path = dependency .. "/lua/?.lua;" .. dependency .. "/lua/?/init.lua;" .. package.path
-- Independently configured owner: the bridge exists before Naia is even loaded.
dofile(assert(vim.env.MCP_TOOLS_RUNTIME)).install()
local mcp = require("mcp-tools")
mcp.setup({ integrations = { opencode = false }, bridge = { tool_timeout = 300000 } })
mcp.start()
assert(vim.wait(15000, function() return mcp.get_port() ~= nil end, 20))
assert(not package.loaded.naia, "mcp-tools must run without Naia")
local naia = require("naia")
local _, err = naia.setup()
assert(not err, err)
assert(naia.is_ready(), naia.last_error())
assert(naia.register_tool("echo", {
  description = "Strict schema echo",
  args = { mode = { type = "string", required = true, description = "One of: one, two." } },
  callback = function(args)
    assert(args.mode == "one" or args.mode == "two", "Invalid mode")
    return vim.env.NAIA_TEST_MARKER .. ":" .. args.mode
  end,
}))
assert(naia.register_tool("object", {
  callback = function() return { status = "ok", marker = vim.env.NAIA_TEST_MARKER } end,
}))
assert(naia.register_tool("fail", { callback = function() error("intentional failure") end }))
assert(naia.register_tool("slow", { callback = function()
  vim.wait(100, function() return false end, 10)
  return "slow result"
end }))
assert(naia.register_tool("nvim_already_prefixed", { callback = function() return "unchanged" end }))
assert(naia.register_tool("add_tool", { callback = function()
  assert(naia.register_tool("dynamic", { callback = function() return "dynamic result" end }))
  return "added"
end }))
assert(naia.register_tool("remove_tool", { callback = function()
  assert(naia.deregister_tool("dynamic"))
  return "removed"
end }))
-- Other providers share the same independently owned server.
mcp.register({ name = "third_party", description = "Not a Naia tool", args = {},
  execute = function(cb) cb("independent provider") end })
vim.g.ai_voice_enabled = false
vim.cmd("filetype plugin off") -- Isolated test runtime has no Tree-sitter parsers.
require("naia.walkthrough").setup()
local info = naia.connection_info()
info.socket = vim.v.servername
local path = assert(vim.env.NAIA_TEST_CONNECTION_OUT)
local fd = assert(vim.uv.fs_open(path, "wx", 384))
assert(vim.uv.fs_write(fd, vim.json.encode(info)))
vim.uv.fs_close(fd)
vim.wait(120000, function() return vim.g.naia_test_done end, 20)
vim.cmd("qa!")
