-- First-party editor feature; mcp-tools owns the independently configured bridge.
local _, err = require("naia").setup()
assert(not err, err)
require("naia.commands").setup()
require("naia.tools").setup()
require("naia.walkthrough").setup()
