local naia_tool_names = {
  inform_user = "inform_user",
  execute_user_command = "execute_user_command",
}

local function notify_naia_tool_error(err)
  vim.schedule(function()
    vim.notify("Naia tool error: " .. tostring(err), vim.log.levels.WARN)
  end)
end

local function notify_naia_tool_registration_error(tool_name, err)
  vim.schedule(function()
    vim.notify("Failed to register Naia `" .. tool_name .. "` tool: " .. tostring(err), vim.log.levels.WARN)
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

local function naia_execute_user_command(args)
  local command_line = args and args.command or nil

  if type(command_line) ~= "string" then
    command_line = tostring(command_line or "")
  end

  command_line = vim.trim(command_line)
  if command_line == "" then
    error("execute_user_command requires a non-empty command")
  end

  command_line = command_line:gsub("^:", "")

  local accepted = vim.fn.confirm(
    "Allow Naia to run this command?\n\n:" .. command_line,
    "&Yes\n&No",
    2
  ) == 1

  if not accepted then
    return string.format("User rejected executing the command: :%s", command_line)
  end

  local ok, result = pcall(vim.api.nvim_exec2, command_line, { output = true })
  if not ok then
    error(result)
  end

  local output = result and result.output or ""
  output = vim.trim(output)

  if output == "" then
    return string.format("Executed command: :%s", command_line)
  end

  return output
end

local function register_naia_tools()
  local ok, naia = pcall(require, "naia")
  if not ok then
    return
  end

  for _, tool_name in pairs(naia_tool_names) do
    pcall(naia.deregister_tool, tool_name)
  end

  local inform_registered, inform_err = naia.register_tool(naia_tool_names.inform_user, {
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

  if not inform_registered then
    notify_naia_tool_registration_error(naia_tool_names.inform_user, inform_err)
  end

  local command_registered, command_err = naia.register_tool(naia_tool_names.execute_user_command, {
    title = "Execute Neovim Command",
    description = "Run any Neovim Ex command, if successful it will return its output.",
    input_schema = {
      type = "object",
      properties = {
        command = {
          type = "string",
          description = "The exact Neovim command line to run, without the leading colon.",
        },
      },
      required = { "command" },
      additionalProperties = false,
    },
    callback = naia_execute_user_command,
  })

  if not command_registered then
    notify_naia_tool_registration_error(naia_tool_names.execute_user_command, command_err)
  end
end

register_naia_tools()
