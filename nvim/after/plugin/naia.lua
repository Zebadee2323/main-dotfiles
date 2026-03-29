local naia_tool_name = "inform_user"

local function notify_naia_tool_error(err)
  vim.schedule(function()
    vim.notify("Naia `inform_user` tool error: " .. tostring(err), vim.log.levels.WARN)
  end)
end

local function naia_inform_user(args)
  local message = args and args.message or nil

  if type(message) ~= "string" then
    message = tostring(message or "")
  end

  message = vim.trim(message)
  if message == "" then
    error("inform_user requires a non-empty message")
  end

  vim.schedule(function()
    local ok, err = pcall(vim.api.nvim_cmd, {
      cmd = "AIVoice",
      args = { message },
    }, {})

    if not ok then
      notify_naia_tool_error(err)
    end
  end)

  return ""
end

local function register_naia_inform_user_tool()
  local ok, naia = pcall(require, "naia")
  if not ok then
    return
  end

  pcall(naia.deregister_tool, naia_tool_name)

  local registered, err = naia.register_tool(naia_tool_name, {
    title = "Inform User",
    description = "Notify the user in Neovim and speak the message aloud.",
    input_schema = {
      type = "object",
      properties = {
        message = {
          type = "string",
          description = "The user-facing message to announce.",
        },
      },
      required = { "message" },
      additionalProperties = false,
    },
    callback = naia_inform_user,
  })

  if not registered then
    vim.schedule(function()
      vim.notify("Failed to register Naia `inform_user` tool: " .. tostring(err), vim.log.levels.WARN)
    end)
  end
end

register_naia_inform_user_tool()
