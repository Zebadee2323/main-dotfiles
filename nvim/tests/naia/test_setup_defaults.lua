local naia = require("naia")
local config, err = naia.setup()
assert(not err and next(config) == nil)
assert(not naia.is_ready() and naia.status().bridge.owned == false)
assert(naia.status().tool_count == 1)
assert(naia.setup() and naia.status().tool_count == 1)
for _, key in ipairs({ "child_cmd", "host", "port", "startup_policy", "log_level", "log_file", "startup_timeout_ms", "shutdown_timeout_ms" }) do
  assert(not pcall(naia.setup, { [key] = "configure upstream instead" }))
end
