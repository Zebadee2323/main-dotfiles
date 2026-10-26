local naia = require("naia")
local mcp = require("mcp-tools")
local running, port, token = false, nil, nil
local forbidden = function() error("Naia must not manage a process, socket or credential file") end
vim.system, vim.fn.serverstart, vim.fn.serverstop = forbidden, forbidden, forbidden
vim.uv.fs_open, vim.uv.new_timer = forbidden, forbidden
package.loaded["mcp-tools.bridge"] = {
  start = forbidden, stop = forbidden,
  is_running = function() return running end,
  get_port = function() return port end,
  get_auth_token = function() return token end,
}
local _, err = naia.setup()
assert(not err and not running and not naia.is_ready())
assert(mcp.list_tools().get_working_directory)
assert(not mcp.list_tools().naia_catalog and not mcp.list_tools().naia_dispatch)
assert(naia.register_tool("choice", {
  args = { mode = { type = "string", required = true, description = "One of: one, two." } },
  callback = function(args)
    assert(args.mode == "one" or args.mode == "two", "Invalid mode")
    return args.mode
  end,
}))
local result
mcp.list_tools().choice.execute(function(value) result = value end, { mode = "two" })
assert(result == "two", "No compatibility result envelope")
assert(not pcall(mcp.list_tools().choice.execute, function() end, { mode = "invalid" }))
assert(not naia.connection_info(), "An absent bridge must not be started implicitly")
running, port, token = true, 43210, "existing-upstream-token"
assert(naia.is_ready() and naia.status().bridge.owned == false)
assert(naia.status().child == nil and naia.relay_host == nil and naia.relay_port == nil,
  "The first-party provider must not advertise a child process or relay")
local info = assert(naia.connection_info())
assert(info.url == "http://127.0.0.1:43210/" and info.token == token)
assert(info.connection_file == nil and not vim.inspect(naia.status()):find(token, 1, true))
mcp.register({ name = "third_party", description = "Independent", args = {}, execute = function(cb) cb("other") end })
assert(naia.stop() and running and port == 43210)
assert(not mcp.list_tools().choice and mcp.list_tools().third_party)
assert(naia.start() and mcp.list_tools().choice and naia.is_ready())
assert(naia.restart() and running and token == "existing-upstream-token")
assert(naia.deregister_tool("choice") and not mcp.list_tools().choice)
assert(not naia.register_tool("third_party", { callback = function() end }))
assert(not naia.register_tool("legacy_schema", { input_schema = {}, callback = function() end }))
running, port, token = false, nil, nil
assert(not naia.is_ready() and not naia.connection_info())
running, port, token = true, 43211, "rotated-by-config"
assert(naia.connection_info().token == token, "Discovery reads current credentials without observing or owning them")
for _, name in ipairs({ "NaiaStart", "NaiaRestart", "NaiaStop" }) do vim.cmd(name); assert(running) end
assert(mcp.list_tools().third_party)
mcp.register({ name = "get_working_directory", description = "Foreign provider", args = {}, execute = function() end })
local ok, collision = naia.start()
assert(not ok and collision:find("already registered", 1, true))
assert(not naia.status().enabled and mcp.list_tools().get_working_directory.description == "Foreign provider")
mcp.unregister("get_working_directory")
assert(naia.start())
print("thin native mcp-tools provider tests passed")
