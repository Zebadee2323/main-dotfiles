local codecompanion = require("codecompanion")

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
