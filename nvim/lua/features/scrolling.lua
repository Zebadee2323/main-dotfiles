local M = {}
local initialized = false
local DESIRED_SCROLL = 30

-- 'scroll' must be between zero and window height minus one.
local function clamp_scroll()
  local h = vim.api.nvim_win_get_height(0)
  vim.opt.scroll = math.min(DESIRED_SCROLL, math.max(0, h - 1))
end

-- Keep the cursor screen row where possible; continue moving at window edges.
local function scroll_by_option(dir)
  local count = (vim.v.count > 0) and vim.v.count or 1
  local base = vim.o.scroll
  if base <= 0 then
    local h = vim.api.nvim_win_get_height(0)
    base = math.max(1, math.floor(h / 2) - 1)
  end

  local n = base * count
  local view = vim.fn.winsaveview()
  local topline = view.topline
  local lnum = view.lnum
  local last = vim.fn.line("$")
  local height = vim.api.nvim_win_get_height(0)
  local max_down = math.max(0, last - (topline + height - 1))
  local max_up = math.max(0, topline - 1)
  local can_scroll = (dir > 0) and max_down or max_up
  local scroll_amt = math.min(n, can_scroll)
  local new_top = topline + dir * scroll_amt
  local new_lnum = math.max(1, math.min(last, lnum + dir * n))
  local max_top = math.max(1, last - height + 1)
  new_top = math.max(1, math.min(max_top, new_top))

  vim.fn.winrestview({
    topline = new_top,
    lnum = new_lnum,
    col = view.col,
    curswant = view.curswant,
  })
end

function M.setup()
  if initialized then return end
  initialized = true
  vim.api.nvim_create_autocmd({ "UIEnter", "VimEnter", "VimResized", "WinEnter" }, {
    group = vim.api.nvim_create_augroup("UserScrolling", { clear = true }),
    callback = clamp_scroll,
  })
  vim.keymap.set("n", "<C-d>", function() scroll_by_option(1) end,
    { desc = "Scroll down N lines, keep cursor row" })
  vim.keymap.set("n", "<C-u>", function() scroll_by_option(-1) end,
    { desc = "Scroll up N lines, keep cursor row" })
  vim.keymap.set("n", "<C-b>", "<Nop>")
end

return M
