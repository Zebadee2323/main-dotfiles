local helpers = dofile("tests/naia/helpers.lua")

local naia = require("naia")
local state = require("naia.state")

state.reset()
naia.setup()

local ok, err = naia.register_tool("tool_one", {
  description = "Test tool",
  args = {},
  callback = function()
    return "ok"
  end,
})

helpers.truthy(ok, err)
helpers.truthy(state.tools.tool_one ~= nil, "tool should be stored in the registry")

ok, err = naia.register_tool("tool_one", {
  description = "Duplicate",
  args = {},
  callback = function()
    return "duplicate"
  end,
})

helpers.eq(ok, nil, "duplicate tool names should be rejected")
helpers.truthy(err:match("already registered"), "duplicate registrations should explain the failure")

ok, err = naia.deregister_tool("tool_one")
helpers.truthy(ok, err)
helpers.eq(state.tools.tool_one, nil, "deregister should remove the tool from the registry")

ok, err = naia.register_tool("bad_tool", {})
helpers.eq(ok, nil, "tool registration should require a callback")
helpers.truthy(err:match("callback function"), "missing callbacks should be rejected")
