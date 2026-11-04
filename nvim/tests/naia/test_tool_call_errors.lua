local naia = require("naia")
local mcp = require("mcp-tools")
naia.setup()
assert(naia.register_tool("failing", { callback = function() error("intentional failure") end }))
-- The upstream registry catches these exceptions and formats its normal isError result.
local ok, err = pcall(mcp.list_tools().failing.execute, function() end, {})
assert(not ok and tostring(err):find("intentional failure", 1, true))
assert(naia.deregister_tool("failing") and not mcp.list_tools().failing)
assert(naia.register_tool("mutate_args", {
  callback = function(args) args.message = "changed"; return false end,
}))
local args, result = { message = "original" }, nil
mcp.list_tools().mutate_args.execute(function(value) result = value end, args)
assert(result == false and args.message == "original")
assert(naia.register_tool("nothing", { callback = function() end }))
mcp.list_tools().nothing.execute(function(value) result = value end, {})
assert(result == "")
assert(naia.register_tool("object", { callback = function() return { status = "ok" } end }))
mcp.list_tools().object.execute(function(value) result = value end, {})
assert(result.status == "ok", "Upstream owns ordinary table-to-JSON formatting")
