local M = {}

function M.setup()
  -- Requiring the runtime once owns watchers, commands, and shutdown cleanup.
  require("features.file_change_fx.runtime")
end

return M
