local codecompanion = require("codecompanion")

local ai_chat_adapter = "opencode"
local ai_chat_model = "openai/gpt-5.4/medium"

local function is_acp_adapter(name)
  local ok, config = pcall(require, "codecompanion.config")

  return ok and config.adapters.acp and config.adapters.acp[name] ~= nil
end

local function create_or_replace_user_command(name, fn, opts)
  pcall(vim.api.nvim_del_user_command, name)
  vim.api.nvim_create_user_command(name, fn, opts)
end

local function open_default_ai_chat()
  local chat_helpers = require("codecompanion.interactions.chat.helpers")
  local params = {
    adapter = ai_chat_adapter,
  }

  if not is_acp_adapter(ai_chat_adapter) then
    params.model = ai_chat_model
  end

  return codecompanion.chat({
    params = params,
    callbacks = {
      on_created = function(chat)
        if chat.adapter.type ~= "acp" then
          return
        end

        chat.adapter.defaults = chat.adapter.defaults or {}
        chat.adapter.defaults.model = ai_chat_model

        chat_helpers.create_acp_connection(chat)

        vim.defer_fn(function()
          if chat.acp_connection then
            chat:change_model({ model = ai_chat_model })
          end
        end, 100)
      end,
    },
  })
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

codecompanion.setup({
  interactions = {
    background = {
      adapter = "opencode",
    },
    chat = {
      adapter = "opencode",
    },
    inline = {
      adapter = "opencode",
    },
    cmd = {
      adapter = "opencode",
    },
  },
  display = {
    chat = {
      fold_reasoning = false,
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

vim.api.nvim_create_user_command("CodeCompanionChatModel", function()
  local bufnr = vim.api.nvim_get_current_buf()
  local metadata = _G.codecompanion_chat_metadata and _G.codecompanion_chat_metadata[bufnr]

  if not metadata and _G.codecompanion_current_context then
    metadata = _G.codecompanion_chat_metadata[_G.codecompanion_current_context]
  end

  local adapter = metadata and metadata.adapter
  if not adapter then
    vim.notify("No active CodeCompanion chat buffer found", vim.log.levels.WARN)
    return
  end

  vim.notify(string.format("CodeCompanion adapter: %s | model: %s", adapter.name, adapter.model))
end, {
  desc = "Show the active CodeCompanion chat adapter and model",
})

create_or_replace_user_command("AI", function()
  toggle_default_ai_chat()
end, {
  desc = "Toggle CodeCompanion chat with OpenCode gpt-5.4 medium",
})
