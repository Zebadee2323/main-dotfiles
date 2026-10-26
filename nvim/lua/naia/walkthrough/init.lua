-- Requiring the public facade must not install commands, tools or autocmds.
local M = {}
local runtime

function M.setup()
  if runtime then return end
  local controller = require("naia.walkthrough.runtime")
  controller.setup()
  runtime = controller
end

function M.stop()
  if runtime then return runtime.stop() end
  return false
end

return M
