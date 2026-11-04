-- Optional CodeCompanion integration. No playback, registrations or globals.
local M = {}

function M.foldtext()
  local labels = vim.b.ai_walkthrough_fold_labels
  local label = type(labels) == "table" and labels[vim.v.foldstart] or nil
  return type(label) == "string" and label ~= "" and label or vim.fn.getline(vim.v.foldstart)
end

local function target_chat()
  local ok, codecompanion = pcall(require, "codecompanion")
  if not ok then return nil, "CodeCompanion is not available" end
  local found, current = pcall(codecompanion.buf_get_chat, 0)
  if found and current and not current.hidden then return current end
  local last = codecompanion.last_chat and codecompanion.last_chat() or nil
  if last and not last.hidden then return last end
  return nil, "No active CodeCompanion chat found"
end

local function send(chat, message)
  if chat.current_request then
    vim.notify("CodeCompanion chat is busy", vim.log.levels.WARN)
    return false
  end
  if not chat.ui:is_visible() then
    chat.ui:open()
  elseif chat.ui.winnr and vim.api.nvim_win_is_valid(chat.ui.winnr) then
    vim.api.nvim_set_current_win(chat.ui.winnr)
  end

  local lines = vim.split(message, "\n", { plain = true, trimempty = false })
  local was_locked = not vim.bo[chat.bufnr].modifiable
  if was_locked and chat.ui.unlock_buf then chat.ui:unlock_buf() end
  local count = vim.api.nvim_buf_line_count(chat.bufnr)
  local last = vim.api.nvim_buf_get_lines(chat.bufnr, count - 1, count, false)[1] or ""
  local first = last == "" and count or count + 2
  local ok, err = pcall(function()
    if last == "" then
      vim.api.nvim_buf_set_lines(chat.bufnr, count - 1, count, false, lines)
    else
      vim.api.nvim_buf_set_lines(chat.bufnr, count, count, false, vim.list_extend({ "" }, lines))
    end
  end)
  if was_locked and chat.ui.lock_buf then chat.ui:lock_buf() end
  if not ok then
    vim.notify("AI walkthrough could not write to the chat buffer: " .. tostring(err), vim.log.levels.WARN)
    return false
  end

  local labels = vim.b[chat.bufnr].ai_walkthrough_fold_labels
  if type(labels) ~= "table" then labels = {} end
  labels[first] = "User prompt for walkthrough:"
  vim.b[chat.bufnr].ai_walkthrough_fold_labels = labels
  if chat.ui.winnr and vim.api.nvim_win_is_valid(chat.ui.winnr) then
    pcall(vim.api.nvim_win_call, chat.ui.winnr, function()
      vim.opt_local.foldmethod = "manual"
      vim.opt_local.foldtext = "v:lua.require'naia.walkthrough.chat'.foldtext()"
      vim.cmd(string.format("%d,%dfold", first, first + #lines - 1))
      vim.cmd(string.format("%dfoldclose", first))
    end)
  end
  if chat.ui:is_visible() then chat.ui:follow() end
  return true
end

function M.instruct(state, pause, opts)
  if not state.active or not state.steps then
    vim.notify("No AI walkthrough is currently running", vim.log.levels.WARN)
    return
  end
  pause()
  local step = state.steps[state.index]
  if not step then
    vim.notify("No current AI walkthrough step is available to instruct against", vim.log.levels.WARN)
    return
  end
  local chat, err = target_chat()
  if not chat then vim.notify(err, vim.log.levels.WARN); return end
  local instruction = vim.trim(opts.args or "")
  return send(chat, table.concat({
    "You are helping with a user's instruction about a single step from an existing text editor walkthrough.",
    "The user's name is Ollie",
    "Do not reply with YAML unless the user explicitly asks for it.",
    "If the best response is to extend the walkthrough itself, use the `nvim_walkthrough_insert_steps` MCP tool.",
    "Use the current walkthrough step as the main context for your response.",
    "Stay tightly scoped to the same part of the codebase unless the instruction clearly requires adjacent context.",
    "", "Current step:",
    string.format("id: %d", step.id),
    "walkthrough_id: " .. tostring(state.walkthrough_id),
    "expected_revision: " .. tostring(state.revision),
    string.format("path: %s", step.path),
    string.format("line_start: %d", step.line_start),
    string.format("line_end: %d", step.line_end),
    string.format("description: %s", step.description),
    "", "User instruction:", instruction,
  }, "\n"))
end

return M
