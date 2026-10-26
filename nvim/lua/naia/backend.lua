-- A thin tool provider for an independently configured mcp-tools bridge.
-- Never call mcp-tools.setup/start/stop or own its process/RPC socket here.
local state = require("naia.state")
local M = {}
local owned = {}

local function api()
  local ok, mcp = pcall(require, "mcp-tools")
  if not ok then return nil, "Naia requires guill/mcp-tools.nvim; configure it in your Neovim plugins" end
  return mcp
end

function M.register(name, spec)
  if not state.enabled then return true end
  local mcp, err = api()
  if not mcp then return nil, err end
  local existing = mcp.list_tools()[name]
  if existing then return nil, "mcp-tools tool '" .. name .. "' is already registered" end
  local tool = { name = name, description = spec.description or name, args = spec.args or {},
    execute = function(cb, arguments)
      -- Upstream catches callback exceptions and formats normal text/JSON results.
      local result = spec.callback(vim.deepcopy(arguments or {}))
      if result == nil then result = "" end
      cb(result)
    end }
  mcp.register(tool)
  owned[name] = tool
  return true
end

function M.deregister(name)
  local mcp, err = api()
  if not mcp then return nil, err end
  local existing = mcp.list_tools()[name]
  if owned[name] and existing and (existing.description ~= owned[name].description or not vim.deep_equal(existing.args, owned[name].args)) then
    return nil, "mcp-tools tool '" .. name .. "' was replaced by another provider"
  end
  if owned[name] then mcp.unregister(name) end
  owned[name] = nil
  return true
end

function M.start()
  local mcp, err = api()
  if not mcp then state.last_error = err; return nil, err end
  local was_enabled = state.enabled
  state.enabled = true
  for name, spec in pairs(state.tools) do
    if not owned[name] then
      local ok
      ok, err = M.register(name, spec)
      if not ok then
        if not was_enabled then M.stop() end -- Roll back a failed re-attach.
        state.last_error = err
        return nil, err
      end
    end
  end
  state.last_error = nil
  return true
end

function M.stop()
  for name in pairs(owned) do
    local ok, err = M.deregister(name)
    if not ok then state.last_error = err; return nil, err end
  end
  state.enabled = false
  state.last_error = nil
  return true
end

local function bridge_info()
  local ok, bridge = pcall(require, "mcp-tools.bridge")
  if not ok or not bridge.is_running() then return nil end
  local port = bridge.get_port()
  if not port then return nil end
  return { host = "127.0.0.1", port = port, endpoint = string.format("http://127.0.0.1:%d/", port) }, bridge
end

function M.status()
  local info = bridge_info()
  info = info or {}
  info.status = not state.enabled and "detached" or (info.endpoint and "ready" or "unavailable")
  info.owned = false
  info.last_error = state.last_error
  return info
end

-- Sensitive snapshot for launchers only: never include this in status/logs.
function M.connection_info()
  local info, bridge = bridge_info()
  if not info then
    return nil, "mcp-tools bridge is not running; start it from your Neovim configuration"
  end
  local token = bridge.get_auth_token()
  if not token then return nil, "mcp-tools bridge authentication is unavailable" end
  return { url = info.endpoint, token = token }
end
return M
