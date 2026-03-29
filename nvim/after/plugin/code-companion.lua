local codecompanion = require("codecompanion")

local sidekick_naia_wrapper = require("naia").wrapper_path()
local ai_chat_adapter = "opencode_naia"
local ai_chat_model = "openai/gpt-5.4/medium"

local function build_opencode_naia_adapter()
  local adapter = vim.deepcopy(require("codecompanion.adapters.acp.opencode"))

  adapter.name = "opencode_naia"
  adapter.formatted_name = "OpenCode Naia"
  adapter.commands = {
    default = {
      sidekick_naia_wrapper,
      "opencode",
      "acp",
    },
  }

  return adapter
end

do
  local ACPHandler = require("codecompanion.interactions.chat.acp.handler")

  if not ACPHandler._user_supports_acp_session_restore then
    local original_ensure_connection = ACPHandler.ensure_connection

    ACPHandler.ensure_connection = function(self)
      if not self.chat.acp_connection and self.chat.acp_session_id then
        self.chat.acp_connection = require("codecompanion.acp").new({
          adapter = self.chat.adapter,
          session_id = self.chat.acp_session_id,
        })

        local connected = self.chat.acp_connection:connect_and_initialize()
        if not connected then
          return false
        end

        if self.chat.acp_connection.session_id then
          local acp_commands = require("codecompanion.interactions.chat.acp.commands")
          acp_commands.link_buffer_to_session(self.chat.bufnr, self.chat.acp_connection.session_id)
        end

        self.chat:update_metadata()
        return true
      end

      return original_ensure_connection(self)
    end

    ACPHandler._user_supports_acp_session_restore = true
  end
end

local function is_acp_adapter(name)
  local ok, config = pcall(require, "codecompanion.config")

  return ok and config.adapters.acp and config.adapters.acp[name] ~= nil
end

local function create_or_replace_user_command(name, fn, opts)
  pcall(vim.api.nvim_del_user_command, name)
  vim.api.nvim_create_user_command(name, fn, opts)
end

local function get_git_root(path)
  local dir = path and vim.fn.fnamemodify(path, ":p:h") or nil
  local cmd = dir and ("git -C " .. vim.fn.shellescape(dir) .. " rev-parse --show-toplevel")
    or "git rev-parse --show-toplevel"

  local ok, lines = pcall(vim.fn.systemlist, cmd)
  if not ok or type(lines) ~= "table" or #lines == 0 then
    return nil
  end

  local root = lines[1]
  if root == nil or root == "" then
    return nil
  end

  return root
end

local function get_repo_relative_path(bufnr)
  local target_buf = bufnr or 0
  local bufname = vim.api.nvim_buf_get_name(target_buf)
  if bufname == "" then
    return "[No Name]"
  end

  local abs_buf = vim.fn.fnamemodify(bufname, ":p")
  local git_root = get_git_root(abs_buf)

  if git_root then
    local abs_root = vim.fn.fnamemodify(git_root, ":p"):gsub("[/\\]$", "")
    local rel = vim.fs.relpath(abs_root, abs_buf)

    if rel then
      if rel == "" then
        return "."
      end

      return rel
    end
  end

  local cwd_rel = vim.fn.fnamemodify(bufname, ":.")
  if cwd_rel ~= "" then
    return cwd_rel
  end

  return bufname
end

local function resolve_acp_model_id(connection, desired_model)
  if not connection or type(desired_model) ~= "string" or desired_model == "" then
    return nil
  end

  local models = connection.get_models and connection:get_models() or nil
  if not models then
    return nil
  end

  for _, model in ipairs(models.availableModels or {}) do
    if model.modelId == desired_model then
      return model.modelId
    end
  end

  local desired_model_lower = desired_model:lower()

  for _, model in ipairs(models.availableModels or {}) do
    if model.modelId and model.modelId:lower():find(desired_model_lower, 1, true) then
      return model.modelId
    end
  end

  return nil
end

local function apply_default_acp_chat_model(chat, chat_helpers)
  if chat.adapter.type ~= "acp" then
    return
  end

  chat.adapter.defaults = chat.adapter.defaults or {}
  chat.adapter.defaults.model = ai_chat_model

  if not chat.acp_connection then
    chat_helpers.create_acp_connection(chat)
  end

  local connection = chat.acp_connection
  if not connection then
    return
  end

  local model_id = resolve_acp_model_id(connection, ai_chat_model)
  if not model_id then
    vim.schedule(function()
      vim.notify(string.format("OpenCode model `%s` is unavailable for this session", ai_chat_model), vim.log.levels.WARN)
    end)
    chat:update_metadata()
    return
  end

  local models = connection.get_models and connection:get_models() or nil
  if models and models.currentModelId == model_id then
    chat:update_metadata()
    return
  end

  chat:change_model({ model = model_id })
end

local function build_default_ai_chat_opts(opts)
  local chat_helpers = require("codecompanion.interactions.chat.helpers")
  local params = {
    adapter = ai_chat_adapter,
  }

  opts = opts or {}

  if not is_acp_adapter(ai_chat_adapter) then
    params.model = ai_chat_model
  end

  return vim.tbl_deep_extend("force", {
    params = params,
    callbacks = {
      on_created = function(chat)
        apply_default_acp_chat_model(chat, chat_helpers)
      end,
    },
  }, opts)
end

local function build_default_ai_chat_args(opts)
  return build_default_ai_chat_opts(opts or {})
end

local function open_default_ai_chat()
  return codecompanion.chat(build_default_ai_chat_args())
end

local function get_acp_session_args(adapter)
  local config = require("codecompanion.config")
  local session_args = {
    cwd = vim.fn.getcwd(),
    mcpServers = adapter.defaults and adapter.defaults.mcpServers,
  }

  if session_args.mcpServers == "inherit_from_config" and config.mcp.opts.acp_enabled then
    session_args.mcpServers = require("codecompanion.mcp").transform_to_acp()
  end

  return session_args
end

local function restore_ai_chat_session(chat, session, opts)
  opts = opts or {}

  local attempts_remaining = opts.attempts or 40
  local delay = opts.delay or 100
  local chat_helpers = require("codecompanion.interactions.chat.helpers")
  local acp_commands = require("codecompanion.interactions.chat.acp.commands")
  local acp_methods = require("codecompanion.acp.methods")

  local function try_restore()
    if not chat or not session or chat.adapter.type ~= "acp" then
      return
    end

    if not chat.acp_connection then
      chat_helpers.create_acp_connection(chat)
    end

    local connection = chat.acp_connection
    if not connection or not connection.session_id then
      attempts_remaining = attempts_remaining - 1
      if attempts_remaining <= 0 then
        vim.notify("Timed out waiting for the OpenCode session to initialize", vim.log.levels.ERROR)
        return
      end

      vim.defer_fn(try_restore, delay)
      return
    end

    local ok, result = pcall(connection.send_rpc_request, connection, acp_methods.SESSION_LOAD, vim.tbl_extend(
      "force",
      get_acp_session_args(chat.adapter),
      { sessionId = session.id }
    ))

    if not ok or result == nil then
      vim.notify(string.format("Failed to restore OpenCode session %s", session.id), vim.log.levels.ERROR)
      return
    end

    connection.session_id = session.id
    chat.acp_session_id = session.id

    if type(result) == "table" then
      if result.models then
        connection._models = result.models
      end
      if result.modes then
        connection._modes = result.modes
      end
    end

    acp_commands.link_buffer_to_session(chat.bufnr, session.id)
    apply_default_acp_chat_model(chat, chat_helpers)
    chat:set_title(string.format("[CodeCompanion] %s", session.title))
    chat:update_metadata()

    vim.notify(string.format("Restored OpenCode session %s", session.id), vim.log.levels.INFO)
  end

  try_restore()
end

local function open_default_ai_chat_session(session)
  local chat = require("codecompanion.interactions.chat").new(vim.tbl_deep_extend("force", build_default_ai_chat_args(), {
    title = string.format("[CodeCompanion] %s", session.title),
  }))

  if chat then
    restore_ai_chat_session(chat, session)
  end

  return chat
end

local function toggle_default_ai_chat()
  local chat_api = require("codecompanion.interactions.chat")
  local current_buf = vim.api.nvim_get_current_buf()
  local current_chat = nil

  if _G.codecompanion_chat_metadata and _G.codecompanion_chat_metadata[current_buf] then
    current_chat = codecompanion.buf_get_chat(current_buf)
  end

  if current_chat and current_chat.ui:is_visible() then
    current_chat.ui:hide()
    return
  end

  local last_chat = chat_api.last_chat()
  if last_chat then
    last_chat.ui:open({ toggled = true })
    return
  end

  open_default_ai_chat()
end

local function get_target_ai_chat()
  local chat_api = require("codecompanion.interactions.chat")
  local current_buf = vim.api.nvim_get_current_buf()

  if _G.codecompanion_chat_metadata and _G.codecompanion_chat_metadata[current_buf] then
    return codecompanion.buf_get_chat(current_buf)
  end

  return chat_api.last_chat()
end

local function set_current_ai_chat_model(opts)
  opts = opts or {}

  local chat = get_target_ai_chat()
  if not chat then
    vim.notify("No active CodeCompanion chat found", vim.log.levels.WARN)
    return
  end

  local desired_model = vim.trim(opts.args or "")

  if chat.adapter.type == "acp" then
    local chat_helpers = require("codecompanion.interactions.chat.helpers")

    if not chat.acp_connection then
      chat_helpers.create_acp_connection(chat)
    end

    local connection = chat.acp_connection
    if not connection then
      vim.notify("OpenCode session is not connected", vim.log.levels.ERROR)
      return
    end

    local models = connection.get_models and connection:get_models() or nil
    local available_models = models and models.availableModels or {}

    if vim.tbl_isempty(available_models) then
      vim.notify("No models reported for this OpenCode session", vim.log.levels.WARN)
      return
    end

    local function apply_model(model_id)
      chat.adapter.defaults = chat.adapter.defaults or {}
      chat.adapter.defaults.model = model_id
      chat:change_model({ model = model_id })
      vim.notify(string.format("CodeCompanion model set to %s", model_id), vim.log.levels.INFO)
    end

    if desired_model ~= "" then
      local resolved_model = resolve_acp_model_id(connection, desired_model)
      if not resolved_model then
        vim.notify(string.format("Model `%s` is unavailable for this OpenCode session", desired_model), vim.log.levels.WARN)
        return
      end

      apply_model(resolved_model)
      return
    end

    vim.ui.select(available_models, {
      prompt = "Select CodeCompanion model",
      format_item = function(item)
        return item.modelId
      end,
    }, function(choice)
      if not choice or not choice.modelId then
        return
      end

      apply_model(choice.modelId)
    end)

    return
  end

  if desired_model == "" then
    vim.notify("Provide a model name, for example: :AIModel openai/gpt-5.4/medium", vim.log.levels.INFO)
    return
  end

  chat.adapter.model = desired_model
  chat.adapter.defaults = chat.adapter.defaults or {}
  chat.adapter.defaults.model = desired_model
  if chat.change_model then
    chat:change_model({ model = desired_model })
  else
    chat:update_metadata()
  end
  vim.notify(string.format("CodeCompanion model set to %s", desired_model), vim.log.levels.INFO)
end

local function build_ai_send_message(opts)
  opts = opts or {}
  local prefix = vim.trim(opts.args or "")
  local path = get_repo_relative_path(opts.bufnr)
  local body

  if opts.range and opts.line1 and opts.line2 and opts.line2 >= opts.line1 then
    if opts.line1 == opts.line2 then
      body = string.format("@%s %d", path, opts.line1)
    else
      body = string.format("@%s %d-%d", path, opts.line1, opts.line2)
    end
  else
    local line = opts.current_line or vim.api.nvim_win_get_cursor(0)[1]
    body = string.format("@%s %d", path, line)
  end

  if prefix ~= "" then
    return prefix .. "\n\n" .. body
  end

  return body
end

local function send_message_to_chat(chat, msg)
  if chat.current_request then
    vim.notify("CodeCompanion chat is busy", vim.log.levels.WARN)
    return
  end

  if not chat.ui:is_visible() then
    chat.ui:open()
  elseif chat.ui.winnr and vim.api.nvim_win_is_valid(chat.ui.winnr) then
    vim.api.nvim_set_current_win(chat.ui.winnr)
  end

  local message_lines = vim.split(msg, "\n", { plain = true, trimempty = false })
  local line_count = vim.api.nvim_buf_line_count(chat.bufnr)
  local last_line = vim.api.nvim_buf_get_lines(chat.bufnr, line_count - 1, line_count, false)[1] or ""

  if last_line == "" then
    vim.api.nvim_buf_set_lines(chat.bufnr, line_count - 1, line_count, false, message_lines)
  else
    vim.api.nvim_buf_set_lines(chat.bufnr, line_count, line_count, false, vim.list_extend({ "" }, message_lines))
  end

  if chat.ui:is_visible() then
    chat.ui:follow()
  end
end

local function send_to_codecompanion(opts)
  local msg = build_ai_send_message(opts)
  local chat = get_target_ai_chat()

  if not chat then
    chat = open_default_ai_chat()
  end

  if chat then
    send_message_to_chat(chat, msg)
  end
end

local function send_plain_message_to_codecompanion(opts)
  local msg = vim.trim(opts.args or "")

  if msg == "" then
    vim.notify("AIMessage requires a message", vim.log.levels.ERROR)
    return
  end

  local chat = get_target_ai_chat()

  if not chat then
    chat = open_default_ai_chat()
  end

  if chat then
    send_message_to_chat(chat, msg)
  end
end

local function list_opencode_sessions()
  local lines = vim.fn.systemlist("opencode session list")
  if vim.v.shell_error ~= 0 then
    vim.notify("Failed to list OpenCode sessions", vim.log.levels.ERROR)
    return {}
  end

  local sessions = {}
  for _, line in ipairs(lines) do
    if line:match("^ses_") then
      local id, rest = line:match("^(ses_%S+)%s+(.+)$")
      if id and rest then
        local title, updated = rest:match("^(.-)%s%s+([^%s].-)%s*$")
        table.insert(sessions, {
          id = id,
          title = vim.trim(title or rest),
          updated = vim.trim(updated or ""),
        })
      end
    end
  end

  return sessions
end

local function restore_opencode_session()
  local sessions = list_opencode_sessions()
  if vim.tbl_isempty(sessions) then
    vim.notify("No OpenCode sessions found", vim.log.levels.WARN)
    return
  end

  vim.ui.select(sessions, {
    prompt = "Restore OpenCode session",
    format_item = function(item)
      if item.updated ~= "" then
        return string.format("%s [%s]", item.title, item.updated)
      end

      return item.title
    end,
  }, function(choice)
    if not choice then
      return
    end

    open_default_ai_chat_session(choice)
  end)
end

codecompanion.setup({
  adapters = {
    acp = {
      opencode_naia = build_opencode_naia_adapter,
    },
  },
  interactions = {
    background = {
      adapter = ai_chat_adapter,
    },
    chat = {
      adapter = ai_chat_adapter,
    },
    inline = {
      adapter = ai_chat_adapter,
    },
    cmd = {
      adapter = ai_chat_adapter,
    },
  },
  display = {
    chat = {
      fold_reasoning = true,
      show_reasoning = true,
      window = {
        full_height = true,
        layout = "vertical",
        position = "right",
        width = 0.33,
      },
    },
  },
  opts = {
    log_level = "DEBUG",
  },
})

-- create_or_replace_user_command("AI", function()
--   toggle_default_ai_chat()
-- end, {
--   desc = "Toggle CodeCompanion chat with OpenCode gpt-5.4 medium",
-- })

-- create_or_replace_user_command("AISend", function(opts)
--   send_to_codecompanion(opts)
-- end, {
--   nargs = "*",
--   range = true,
--   desc = "Append file line or range to CodeCompanion input",
-- })

-- create_or_replace_user_command("AIWalkSend", function(opts)
--   local send_opts = vim.tbl_extend("force", opts or {}, {
--     bufnr = vim.api.nvim_get_current_buf(),
--     current_line = vim.api.nvim_win_get_cursor(0)[1],
--   })

--   send_to_codecompanion(send_opts)
-- end, {
--   nargs = "*",
--   range = true,
--   desc = "Append file line or range so you can ask for a walkthrough naturally",
-- })

-- create_or_replace_user_command("AIMessage", function(opts)
--   send_plain_message_to_codecompanion(opts)
-- end, {
--   nargs = "+",
--   desc = "Send a plain message to CodeCompanion",
-- })

-- create_or_replace_user_command("AICommit", function()
--   vim.cmd("AIMessage git commit staged")
-- end, {
--   desc = "Send git commit staged prompt to CodeCompanion",
-- })

-- create_or_replace_user_command("AIRestore", function()
--   restore_opencode_session()
-- end, {
--   desc = "List and restore an OpenCode session in CodeCompanion",
-- })

-- create_or_replace_user_command("AIModel", function(opts)
--   set_current_ai_chat_model(opts)
-- end, {
--   nargs = "?",
--   complete = function()
--     local chat = get_target_ai_chat()
--     if not chat or chat.adapter.type ~= "acp" then
--       return {}
--     end

--     local chat_helpers = require("codecompanion.interactions.chat.helpers")
--     if not chat.acp_connection then
--       chat_helpers.create_acp_connection(chat)
--     end

--     local connection = chat.acp_connection
--     local models = connection and connection.get_models and connection:get_models() or nil
--     local items = {}

--     for _, model in ipairs(models and models.availableModels or {}) do
--       table.insert(items, model.modelId)
--     end

--     return items
--   end,
--   desc = "Change the active CodeCompanion chat model",
-- })
