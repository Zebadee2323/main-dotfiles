-- Options ------------------------------------------------------------------------------------------------------------
vim.o.guifont = "LiterationMono Nerd Font Mono:h13"

vim.opt.expandtab = true
vim.opt.tabstop = 4
vim.opt.softtabstop = 4
vim.opt.shiftwidth = 4
vim.opt.autoindent = true
vim.opt.smartindent = true

-- NOTE:
-- 'scroll' must be between 0 and (window height - 1). On Windows (or small terminals),
-- setting it to 30 at startup can throw: E49: Invalid scroll size.
-- We clamp it whenever UI/window sizes are known/changed.
local DESIRED_SCROLL = 30

local function clamp_scroll()
  local h = vim.api.nvim_win_get_height(0)
  -- valid range is 0..(h-1)
  local v = math.min(DESIRED_SCROLL, math.max(0, h - 1))
  vim.opt.scroll = v
end

vim.api.nvim_create_autocmd({ "UIEnter", "VimEnter", "VimResized", "WinEnter" }, {
  callback = clamp_scroll,
})

vim.opt.textwidth = 120
vim.opt.foldlevel = 99
vim.opt.wrap = false
vim.opt.list = true
vim.opt.colorcolumn = { 120 }

vim.opt.number = true
vim.opt.relativenumber = true

vim.opt.swapfile = false
vim.opt.backup = false

vim.opt.hlsearch = false
vim.opt.incsearch = true

vim.opt.updatetime = 50

vim.opt.mouse = "a"

vim.opt.clipboard = "unnamedplus"

-- Remaps ------------------------------------------------------------------------------------------------------------

-- Tabs
vim.keymap.set("n", "<A-Tab>", "<CMD>tabnext<CR>")

-- Resize windows
vim.keymap.set("n", "<Left>", "<CMD>vertical resize +3<CR>")
vim.keymap.set("n", "<Right>", "<CMD>vertical resize -3<CR>")
vim.keymap.set("n", "<Up>", "<CMD>resize -3<CR>")
vim.keymap.set("n", "<Down>", "<CMD>resize +3<CR>")

-- Move selection up and down
vim.keymap.set("v", "J", ":m '>+1<CR>gv=gv")
vim.keymap.set("v", "K", ":m '<-2<CR>gv=gv")

-- Keep search terms in the middle
vim.keymap.set("n", "n", "nzzzv")
vim.keymap.set("n", "N", "Nzzzv")

-- Copy and Paste
vim.keymap.set("n", "y", '"+y')
vim.keymap.set("v", "y", '"+y')
vim.keymap.set("n", "Y", '"+Y')

vim.keymap.set("n", "p", '"+p')
vim.keymap.set("v", "p", '"+p')
vim.keymap.set("n", "P", '"+P')

vim.keymap.set("n", "d", '"+d')
vim.keymap.set("v", "d", '"+d')
vim.keymap.set("n", "D", '"+D')

vim.keymap.set("n", "<C-k>", "<cmd>cprev<cr>zz")
vim.keymap.set("n", "<C-j>", "<cmd>cnext<cr>zz")

vim.keymap.set("t", "<C-w>h", [[<C-\><C-n><C-w>h]])
vim.keymap.set("t", "<C-w>l", [[<C-\><C-n><C-w>l]])
vim.keymap.set("t", "<C-w>j", [[<C-\><C-n><C-w>j]])
vim.keymap.set("t", "<C-w>k", [[<C-\><C-n><C-w>k]])

vim.api.nvim_create_user_command("TrimWhitespace", function()
  local view = vim.fn.winsaveview()
  vim.cmd([[%s/\s\+$//e]])
  vim.fn.winrestview(view)
  vim.notify("Trailing whitespace trimmed", vim.log.levels.INFO)
end, { desc = "Trim trailing whitespace in current buffer" })

vim.keymap.set("n", "<C-s><C-s><C-s>", vim.cmd.TrimWhitespace)

-- vim.keymap.set('n', 'rn', '<CMD>set relativenumber!<CR>')

vim.api.nvim_create_user_command("Config", ":Oil " .. vim.fn.stdpath("config"), {})
vim.api.nvim_create_user_command("ConfigSafe", ":Vexplore " .. vim.fn.stdpath("config"), {})

-- Scroll by 'scroll' (global) while keeping cursor screen row when possible.
-- When the window can't scroll further, continue moving the cursor so motion never stops.
-- dir: +1 (down, like <C-d>), -1 (up, like <C-u>)
local function scroll_by_option(dir)
  local count = (vim.v.count > 0) and vim.v.count or 1

  -- IMPORTANT: use a numeric value; vim.opt.scroll is an option object.
  local base = vim.o.scroll

  -- When 'scroll' is 0, emulate Vim's half-page-ish behavior
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

  -- How much the window can still scroll
  local max_down = math.max(0, last - (topline + height - 1))
  local max_up = math.max(0, topline - 1)

  local can_scroll = (dir > 0) and max_down or max_up
  local scroll_amt = math.min(n, can_scroll) -- part that moves the window
  local cursor_amt = n                       -- cursor always moves full amount

  -- New window top (only scroll what we can)
  local new_top = topline + dir * scroll_amt
  -- New cursor line (always move full amount; keeps row until we hit edge)
  local new_lnum = math.max(1, math.min(last, lnum + dir * cursor_amt))

  -- Extra safety: topline cannot exceed the last possible topline
  local max_top = math.max(1, last - height + 1)
  new_top = math.max(1, math.min(max_top, new_top))

  vim.fn.winrestview({
    topline = new_top,
    lnum = new_lnum,
    col = view.col,
    curswant = view.curswant,
  })
end

-- Example keymaps (use counts: e.g. 10<C-d> / 10<C-b>)
vim.keymap.set("n", "<C-d>", function() scroll_by_option(1) end, { desc = "Scroll down N lines, keep cursor row" })
vim.keymap.set("n", "<C-u>", function() scroll_by_option(-1) end, { desc = "Scroll up N lines, keep cursor row" })
vim.keymap.set('n', '<C-b>', '<Nop>')

vim.api.nvim_create_user_command("T", function(opts)
  vim.cmd("vsplit | terminal " .. (opts.args or ""))
  vim.cmd("startinsert")
end, {
  nargs = "*",
  complete = "shellcmd",
})
