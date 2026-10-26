local mcp = package.loaded["mcp-tools"]
package.loaded["mcp-tools"] = nil
package.preload["mcp-tools"] = function() error("not installed") end
local naia = require("naia")
local _, err = naia.setup()
assert(err and err:find("guill/mcp-tools.nvim", 1, true))
assert(not naia.is_ready())
assert(not naia.start())
package.preload["mcp-tools"] = nil
package.loaded["mcp-tools"] = mcp
local _, recovered = naia.setup()
assert(not recovered and naia.status().tool_count == 1)
