-- Run: nvim --headless -u NONE -l tests/voice_visualizer_spec.lua
vim.opt.rtp:prepend(vim.fn.getcwd())
vim.o.columns = 160
vim.o.laststatus = 2
vim.o.cmdheight = 1
vim.cmd("wincmd =")
vim.g.ai_voice_visualizer_hide_for_floats = false
local visualizer = require("features.voice.visualizer")

local function voice_window()
  for _, win in ipairs(vim.api.nvim_tabpage_list_wins(0)) do
    if vim.wo[win].winhighlight:find("Normal:NaiaUIVoiceNormal", 1, true) then
      return win
    end
  end
end

local function row(anchor, expected)
  vim.g.ai_voice_visualizer_anchor = anchor
  assert(vim.wait(500, function()
    local win = voice_window()
    return win and vim.api.nvim_win_get_config(win).anchor == anchor
      and vim.api.nvim_win_get_config(win).row == expected
  end, 10), "unexpected placement for " .. anchor .. ": expected row " .. expected
    .. ", got " .. vim.inspect(voice_window() and vim.api.nvim_win_get_config(voice_window()))
    .. ", windows " .. vim.inspect(vim.fn.getwininfo()))
end

local bottom = vim.o.lines - vim.o.cmdheight - 1
visualizer.start({ 0.1, waveform = { 0.1, -0.1 } }, 1)
row("SW", bottom - 1)
row("SE", bottom - 1)

vim.cmd("botright 8new")
local panel = vim.api.nvim_get_current_win()
local panel_top = vim.api.nvim_win_get_position(panel)[1]
row("SW", panel_top - 1)
row("SE", panel_top - 1)
vim.g.ai_voice_visualizer_offset_y = 2
row("SE", panel_top - 2)
vim.g.ai_voice_visualizer_offset_y = nil
row("NW", 1)
row("NE", 1)

-- Resizing and stacking panels should be picked up during playback.
vim.api.nvim_win_set_height(panel, 6)
panel_top = vim.api.nvim_win_get_position(panel)[1]
row("SW", panel_top - 1)
vim.cmd("aboveleft 3new")
local stacked = vim.api.nvim_get_current_win()
row("SW", vim.api.nvim_win_get_position(stacked)[1] - 1)
vim.api.nvim_win_close(stacked, true)
vim.api.nvim_win_set_height(panel, 6)
row("SW", vim.api.nvim_win_get_position(panel)[1] - 1)

-- A bottom panel on the opposite side should not displace the visualiser.
vim.api.nvim_win_close(panel, true)
vim.cmd("vsplit")
vim.cmd("wincmd l")
vim.cmd("belowright 8split")
panel = vim.api.nvim_get_current_win()
row("SW", bottom - 1)
row("SE", vim.api.nvim_win_get_position(panel)[1] - 1)

-- A tall window is not a bottom utility panel, even if it is wide.
vim.api.nvim_win_close(panel, true)
vim.cmd("only")
row("SW", bottom - 1)
visualizer.stop()
print("PASS: voice visualiser bottom panels, anchors, resizing and stacked/local splits")
