-- A non-modal confirmation: never wait for input inside an MCP callback.
local M = {}
local pending

function M.close()
  if pending then pending.finish(false) end
end

function M.open(callback)
  M.close()
  local api = vim.api
  local origin = api.nvim_get_current_win()
  local buf = api.nvim_create_buf(false, true)
  vim.bo[buf].bufhidden = "wipe"
  vim.b[buf].naia_walkthrough_confirmation = true
  api.nvim_buf_set_lines(buf, 0, -1, false, {
    "Your AI walkthrough is ready. Start now?", "", "[Enter / y] Start    [Esc / n] Cancel",
  })
  vim.bo[buf].modifiable = false
  local width = math.min(44, math.max(1, vim.o.columns - 4))
  local height = math.min(3, math.max(1, vim.o.lines - 4))
  local win = api.nvim_open_win(buf, true, {
    relative = "editor", style = "minimal", border = "rounded",
    width = width, height = height,
    row = math.max(0, math.floor((vim.o.lines - height - 2) / 2)),
    col = math.max(0, math.floor((vim.o.columns - width - 2) / 2)),
    noautocmd = true, zindex = 200,
  })
  local request = {}
  local closed_event
  function request.finish(accepted)
    if pending ~= request then return end
    pending = nil
    if closed_event then pcall(api.nvim_del_autocmd, closed_event) end
    local focused = api.nvim_get_current_win() == win
    if api.nvim_win_is_valid(win) then api.nvim_win_close(win, true) end
    if api.nvim_buf_is_valid(buf) then api.nvim_buf_delete(buf, { force = true }) end
    if focused and api.nvim_win_is_valid(origin) then api.nvim_set_current_win(origin) end
    callback(accepted)
  end
  pending = request
  for _, key in ipairs({ "<CR>", "y", "s" }) do
    vim.keymap.set("n", key, function() request.finish(true) end, { buffer = buf, silent = true })
  end
  for _, key in ipairs({ "<Esc>", "n", "q" }) do
    vim.keymap.set("n", key, function() request.finish(false) end, { buffer = buf, silent = true })
  end
  closed_event = api.nvim_create_autocmd("WinClosed", {
    pattern = tostring(win), once = true,
    callback = function() request.finish(false) end,
  })
end

return M
