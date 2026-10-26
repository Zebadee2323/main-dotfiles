local M = {}

function M.reset()
  M.tools = {}
  M.enabled = true
  M.last_error = nil
end

M.reset()
return M
