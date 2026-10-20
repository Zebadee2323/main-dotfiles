local M = {}

function M.setup()
  -- Requiring the runtime once owns command registration and playback cleanup.
  require("features.voice.runtime")
end

return M
