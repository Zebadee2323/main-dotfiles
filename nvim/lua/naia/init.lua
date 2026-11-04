local config = require("naia.config")
local backend = require("naia.backend")
local state = require("naia.state")

local M = {}

function M.setup(user_config)
  local merged = config.setup(user_config)

  if not state.tools.get_working_directory then
    local ok, err = M.register_tool("get_working_directory", {
      description = "Get the Neovim instance's current working directory (absolute path), respecting the current window's :lcd or tab's :tcd override.",
      args = {},
      callback = function()
        return vim.fn.getcwd()
      end,
    })
    if not ok then
      return merged, err
    end
  end

  local ok, err = backend.start()
  if not ok then return merged, err end
  return merged
end

function M.start()
  return backend.start()
end

function M.stop()
  return backend.stop()
end

function M.restart()
  local stopped, stop_err = backend.stop()
  if not stopped then
    return nil, stop_err
  end

  return backend.start()
end

function M.register_tool(name, spec)
  if type(name) ~= "string" or name == "" then
    return nil, "tool name must be a non-empty string"
  end

  if type(spec) ~= "table" then
    return nil, "tool spec must be a table"
  end

  if type(spec.callback) ~= "function" then
    return nil, "tool spec must include a callback function"
  end

  if spec.input_schema ~= nil or spec.output_schema ~= nil then
    return nil, "use mcp-tools 'args' definitions instead of input_schema/output_schema"
  end
  if spec.args ~= nil and type(spec.args) ~= "table" then return nil, "tool args must be a table" end

  if state.tools[name] then
    return nil, string.format("tool '%s' is already registered", name)
  end

  local copy = vim.deepcopy(spec)
  local ok, err = backend.register(name, copy)
  if not ok then state.last_error = err; return nil, err end
  state.tools[name] = copy
  return true
end

function M.deregister_tool(name)
  if not state.tools[name] then
    return nil, string.format("tool '%s' is not registered", name)
  end

  local ok, err = backend.deregister(name)
  if not ok then state.last_error = err; return nil, err end
  state.tools[name] = nil
  return true
end

function M.status()
  return {
    bridge = backend.status(),
    enabled = state.enabled,
    tool_count = vim.tbl_count(state.tools),
    config = config.get(),
  }
end

function M.is_ready()
  return state.enabled and backend.status().status == "ready"
end

-- Sensitive: intended for launchers, not status output or logs.
function M.connection_info()
  return backend.connection_info()
end

function M.server_url()
  return backend.status().endpoint
end

function M.wrapper_path()
  local source = debug.getinfo(1, "S").source
  local path = source:sub(1, 1) == "@" and source:sub(2) or source
  local config_root = vim.fs.dirname(vim.fs.dirname(vim.fs.dirname(path)))

  return vim.fs.normalize(config_root .. "/bin/naia-attach")
end

function M.last_error()
  return state.last_error
end

function M.list_tools()
  local names = {}

  for name in pairs(state.tools) do
    table.insert(names, name)
  end

  table.sort(names)
  return names
end

return M
