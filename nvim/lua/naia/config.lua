local M = {}
local options = {}
local bridge_options = { child_cmd = true, host = true, port = true, startup_policy = true,
  log_level = true, log_file = true, startup_timeout_ms = true, shutdown_timeout_ms = true }

function M.defaults() return {} end
function M.get() return vim.deepcopy(options) end

function M.setup(user_config)
  for key in pairs(user_config or {}) do
    if bridge_options[key] then
      error("Naia no longer owns the bridge; configure '" .. key .. "' in mcp-tools.nvim instead")
    end
    error("unknown naia option: " .. tostring(key))
  end
  options = {}
  return M.get()
end
return M
