local naia_tool_names = {
  inform_user = "inform_user",
  execute_user_command = "execute_user_command",
}

_G.__naia_command_permissions = _G.__naia_command_permissions or {
  allowed_patterns = {},
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
    allowed_patterns = {},
  }

  _G.__naia_command_permissions.allowed_patterns = _G.__naia_command_permissions.allowed_patterns or {}
  return _G.__naia_command_permissions.allowed_patterns
end

local function derive_lua_command_permission_key(command_line)
  local lua_body = command_line:match("^lua%s+(.+)$")
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

local function describe_command_permission_key(permission_key)
  if not permission_key then
    return nil
  end

  return permission_key
    :gsub("^lua:assign:", "Lua assignment: ")
    :gsub("^lua:local_assign:", "Local Lua assignment: ")
    :gsub("^lua:call:", "Lua call: ")
    :gsub("^lua:return:", "Lua return: ")
    :gsub("^lua:exact:", "Exact Lua command: ")
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

  command_line = normalize_command_line(command_line)

  local permission_key = derive_lua_command_permission_key(command_line)
  local allowed_patterns = command_permission_store()
  local already_allowed = permission_key and allowed_patterns[permission_key] == true

  if not already_allowed then
    local prompt = "Allow Naia to run this command?\n\n:" .. command_line
    if permission_key then
      prompt = prompt .. "\n\nAlways-allow group:\n" .. describe_command_permission_key(permission_key)
    end

    local choice = vim.fn.confirm(
      prompt,
      permission_key and "&Yes\n&No\n&Always" or "&Yes\n&No",
      2
    )

    if choice == 2 or choice == 0 then
      return string.format("User rejected executing the command: :%s", command_line)
    end

    if permission_key and choice == 3 then
      allowed_patterns[permission_key] = true
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
