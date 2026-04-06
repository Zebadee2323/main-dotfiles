local naia_tool_names = {
  inform_user = "inform_user",
  execute_neovim_command = "execute_neovim_command",
}

_G.__naia_command_permissions = _G.__naia_command_permissions or {
  allowed_commands = {},
  allowed_details = {},
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

local function normalize_command_line(command_line)
  command_line = vim.trim(command_line or "")
  return command_line:gsub("^:", "")
end

local function command_permission_store()
  _G.__naia_command_permissions = _G.__naia_command_permissions or {
    allowed_commands = {},
    allowed_details = {},
  }

  _G.__naia_command_permissions.allowed_commands = _G.__naia_command_permissions.allowed_commands or {}
  _G.__naia_command_permissions.allowed_details = _G.__naia_command_permissions.allowed_details or {}

  return _G.__naia_command_permissions
end

local function extract_command_name(command_line)
  local name = command_line:match("^(%S+)")
  if not name then
    return nil
  end

  return name:lower()
end

local function derive_lua_detail_key(lua_body)
  if not lua_body then
    return nil
  end

  lua_body = vim.trim(lua_body)
  if lua_body == "" then
    return nil
  end

  local assignment_target = lua_body:match("^([%w_%.%[%]'\"%-]+)%s*=[^=]")
  if assignment_target then
    return "lua:assign:" .. assignment_target
  end

  local local_assignment_target = lua_body:match("^local%s+([%w_]+)%s*=[^=]")
  if local_assignment_target then
    return "lua:local_assign:" .. local_assignment_target
  end

  local call_target = lua_body:match("^([%w_%.:]+)%s*%b()")
  if call_target then
    return "lua:call:" .. call_target
  end

  local member_target = lua_body:match("^return%s+([%w_%.:]+)")
  if member_target then
    return "lua:return:" .. member_target
  end

  return "lua:exact:" .. lua_body
end

local function derive_command_detail_key(command_line)
  local command_name = extract_command_name(command_line)
  if not command_name then
    return nil
  end

  if command_name == "lua" then
    return derive_lua_detail_key(command_line:match("^lua%s+(.+)$"))
  end

  if command_name == "luafile" then
    local path = vim.trim(command_line:match("^luafile%s+(.+)$") or "")
    if path == "" then
      return "luafile:exact"
    end

    return "luafile:" .. vim.fs.normalize(path)
  end

  if command_name == "source" then
    local path = vim.trim(command_line:match("^source%s+(.+)$") or "")
    if path == "" then
      return "source:exact"
    end

    return "source:" .. vim.fs.normalize(path)
  end

  return nil
end

local function describe_permission_key(permission_key)
  if not permission_key then
    return nil
  end

  return permission_key
    :gsub("^cmd:", "Command: :")
    :gsub("^lua:assign:", "Lua assignment: ")
    :gsub("^lua:local_assign:", "Local Lua assignment: ")
    :gsub("^lua:call:", "Lua call: ")
    :gsub("^lua:return:", "Lua return: ")
    :gsub("^lua:exact:", "Exact Lua command: ")
    :gsub("^luafile:", "Lua file: ")
    :gsub("^source:", "Source file: ")
end

local function naia_execute_user_command(args)
  local command_line = args and args.command or nil

  if type(command_line) ~= "string" then
    command_line = tostring(command_line or "")
  end

  command_line = vim.trim(command_line)
  if command_line == "" then
    error("execute_neovim_command requires a non-empty command")
  end

  command_line = normalize_command_line(command_line)

  local permissions = command_permission_store()
  local command_name = extract_command_name(command_line)
  local command_key = command_name and ("cmd:" .. command_name) or nil
  local detail_key = derive_command_detail_key(command_line)
  local command_allowed = command_key and permissions.allowed_commands[command_key] == true
  local detail_allowed = detail_key and permissions.allowed_details[detail_key] == true

  if not command_allowed and not detail_allowed then
    local prompt = "Allow Naia to run this command?\n\n:" .. command_line
    if command_key then
      prompt = prompt .. "\n\nCommand group:\n" .. describe_permission_key(command_key)
    end
    if detail_key then
      prompt = prompt .. "\n\nDetailed group:\n" .. describe_permission_key(detail_key)
    end

    local buttons = "&Yes\n&No\nAlways &Command"
    if detail_key then
      buttons = buttons .. "\nAlways &Detail"
    end

    local choice = vim.fn.confirm(
      prompt,
      buttons,
      2
    )

    if choice == 2 or choice == 0 then
      return string.format("User rejected executing the command: :%s", command_line)
    end

    if choice == 3 and command_key then
      permissions.allowed_commands[command_key] = true
    end

    if detail_key and choice == 4 then
      permissions.allowed_details[detail_key] = true
    end
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

  local command_registered, command_err = naia.register_tool(naia_tool_names.execute_neovim_command, {
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
    notify_naia_tool_registration_error(naia_tool_names.execute_neovim_command, command_err)
  end
end

register_naia_tools()
vim.schedule(register_naia_tools)
