local api = vim.api
local uv = vim.uv or vim.loop

local panel_height = 5
local set_target

local state = {
  bufnr = nil,
  winnr = nil,
  target_bufnr = nil,
  target_winnr = nil,
  timer = nil,
  talking = false,
  tick = 0,
}

local ns = api.nvim_create_namespace("AIFace")
local group = api.nvim_create_augroup("AIFace", { clear = true })
vim.g.ai_face_overlay_enabled = vim.g.ai_face_overlay_enabled == true

local function set_hl(name, value)
  api.nvim_set_hl(0, name, value)
end

local function create_or_replace_user_command(name, fn, opts)
  pcall(api.nvim_del_user_command, name)
  api.nvim_create_user_command(name, fn, opts)
end

set_hl("AIFaceNormal", { fg = "#dbeafe", bg = "#0f1724" })
set_hl("AIFaceBorder", { fg = "#4ea6ff", bg = "#0f1724" })
set_hl("AIFaceWave", { fg = "#315b7b", bg = "#0f1724" })
set_hl("AIFacePulse", { fg = "#6fd3ff", bg = "#0f1724", bold = true })

local function is_valid_win(win)
  return win and api.nvim_win_is_valid(win)
end

local function is_valid_buf(buf)
  return buf and api.nvim_buf_is_valid(buf)
end

local function create_canvas(width, height)
  local canvas = {}

  for row = 1, height do
    local chars = {}
    for col = 1, width do
      chars[col] = " "
    end
    canvas[row] = chars
  end

  return canvas
end

local function canvas_to_lines(canvas)
  local lines = {}

  for row, chars in ipairs(canvas) do
    lines[row] = table.concat(chars)
  end

  return lines
end

local function build_wave_canvas(width)
  local canvas = create_canvas(width, panel_height)
  local center = math.floor((panel_height + 1) / 2)
  local phase = state.tick * (state.talking and 0.85 or 0.22)
  local amplitude = state.talking and 1.75 or 1.05
  local shimmer = state.talking and 0.38 or 0.16
  local pulse_step = state.talking and 6 or 10
  local previous_sample = nil

  for col = 1, width do
    local sample = math.sin((col / 4.5) + phase) * 0.65
      + math.sin((col / 9) - (phase * 1.35)) * 0.25
      + math.sin((col / 15) + (phase * 0.45)) * 0.1
      + math.sin((col / 2.6) - (phase * 0.75)) * shimmer
    local ripple = math.sin((col / pulse_step) - (phase * 1.8)) * (state.talking and 0.55 or 0.18)
    sample = sample + ripple
    local energy = math.floor(math.abs(sample) * amplitude + 0.5)
    local top = math.max(1, center - energy)
    local bottom = math.min(panel_height, center + energy)
    local slope = previous_sample and (sample - previous_sample) or 0

    for row = top, bottom do
      local dist = math.abs(row - center)
      local ch

      if dist == 0 then
        if state.talking and math.abs(slope) > 0.16 then
          ch = slope > 0 and "/" or "\\"
        else
          ch = state.talking and "=" or "-"
        end
      elseif dist == energy then
        if state.talking and energy >= 2 then
          ch = "*"
        else
          ch = "."
        end
      else
        if math.abs(slope) > 0.22 then
          ch = slope > 0 and "/" or "\\"
        else
          ch = state.talking and "~" or ":"
        end
      end

      canvas[row][col] = ch
    end

    if state.talking and energy > 0 then
      if top > 1 then
        canvas[top - 1][col] = "."
      end
      if bottom < panel_height then
        canvas[bottom + 1][col] = "."
      end
    end

    if state.talking and energy > 0 and ((col + state.tick) % 11 == 0) then
      canvas[center][col] = "o"
    elseif (not state.talking) and energy == 0 and ((col + state.tick) % 17 == 0) then
      canvas[center][col] = "."
    end

    previous_sample = sample
  end

  return canvas
end

local function wave_group(ch)
  if ch == " " then
    return nil
  end

  if ch == "=" or ch == "~" or ch == "/" or ch == "\\" or ch == "o" then
    return "AIFacePulse"
  end

  return "AIFaceWave"
end

local function highlight_runs(bufnr, lines)
  api.nvim_buf_clear_namespace(bufnr, ns, 0, -1)

  for row_index, line in ipairs(lines) do
    local run_group = nil
    local run_start = nil

    for col = 1, #line + 1 do
      local ch = col <= #line and line:sub(col, col) or " "
      local group = nil

      if col <= #line then
        group = wave_group(ch)
      end

      if group ~= run_group then
        if run_group then
          api.nvim_buf_add_highlight(bufnr, ns, run_group, row_index - 1, run_start - 1, col - 1)
        end

        run_group = group
        run_start = group and col or nil
      end
    end
  end
end

local function ensure_buf()
  if is_valid_buf(state.bufnr) then
    return state.bufnr
  end

  local bufnr = api.nvim_create_buf(false, true)
  state.bufnr = bufnr

  vim.bo[bufnr].bufhidden = "wipe"
  vim.bo[bufnr].buflisted = false
  vim.bo[bufnr].buftype = "nofile"
  vim.bo[bufnr].filetype = "aiface"
  vim.bo[bufnr].modifiable = false
  vim.bo[bufnr].swapfile = false

  return bufnr
end

local function stop_timer()
  if state.timer then
    state.timer:stop()
    state.timer:close()
    state.timer = nil
  end
end

local function close_face_window()
  if is_valid_win(state.winnr) then
    pcall(api.nvim_win_close, state.winnr, true)
  end

  state.winnr = nil
end

local function clear_target()
  state.target_bufnr = nil
  state.target_winnr = nil
  close_face_window()
  stop_timer()
end

local function is_enabled()
  return vim.g.ai_face_overlay_enabled == true
end

local function set_enabled(enabled)
  vim.g.ai_face_overlay_enabled = enabled == true

  if not vim.g.ai_face_overlay_enabled then
    clear_target()
  elseif is_valid_buf(state.target_bufnr) then
    vim.schedule(function()
      set_target(state.target_bufnr)
    end)
  end

  vim.notify(
    "AI face overlay is now " .. (vim.g.ai_face_overlay_enabled and "enabled" or "disabled"),
    vim.log.levels.INFO
  )
end

local function configure_face_window(win)
  vim.wo[win].winfixheight = true
  vim.wo[win].winhl = "Normal:AIFaceNormal,NormalNC:AIFaceNormal,EndOfBuffer:AIFaceNormal,WinSeparator:AIFaceBorder"
  vim.wo[win].number = false
  vim.wo[win].relativenumber = false
  vim.wo[win].signcolumn = "no"
  vim.wo[win].statuscolumn = ""
  vim.wo[win].foldcolumn = "0"
  vim.wo[win].spell = false
  vim.wo[win].list = false
  vim.wo[win].wrap = false
  vim.wo[win].cursorline = false
  vim.wo[win].winbar = ""
  vim.wo[win].fillchars = "eob: "

  pcall(api.nvim_win_set_height, win, panel_height)
end

local function ensure_window()
  if not is_valid_win(state.target_winnr) then
    clear_target()
    return nil
  end

  local bufnr = ensure_buf()

  if is_valid_win(state.winnr) then
    if api.nvim_win_get_buf(state.winnr) ~= bufnr then
      api.nvim_win_set_buf(state.winnr, bufnr)
    end

    configure_face_window(state.winnr)
    return bufnr
  end

  local current_win = api.nvim_get_current_win()
  local ok, win = pcall(api.nvim_open_win, bufnr, false, {
    split = "above",
    win = state.target_winnr,
    height = panel_height,
  })

  if is_valid_win(current_win) and api.nvim_get_current_win() ~= current_win then
    pcall(api.nvim_set_current_win, current_win)
  end

  if not ok or not is_valid_win(win) then
    return nil
  end

  state.winnr = win
  configure_face_window(win)
  return bufnr
end

local function render()
  if not is_enabled() then
    clear_target()
    return
  end

  if not is_valid_win(state.target_winnr) then
    clear_target()
    return
  end

  local bufnr = ensure_window()
  if not bufnr or not is_valid_win(state.winnr) then
    return
  end

  local width = api.nvim_win_get_width(state.winnr)
  local canvas = build_wave_canvas(width)
  local lines = canvas_to_lines(canvas)

  vim.bo[bufnr].modifiable = true
  api.nvim_buf_set_lines(bufnr, 0, -1, false, lines)
  highlight_runs(bufnr, lines)
  vim.bo[bufnr].modifiable = false
end

local function start_timer()
  if state.timer then
    return
  end

  state.timer = uv.new_timer()
  state.timer:start(0, 120, vim.schedule_wrap(function()
    state.tick = state.tick + 1
    render()
  end))
end

set_target = function(bufnr)
  if not is_enabled() then
    return
  end

  local ok, codecompanion = pcall(require, "codecompanion")
  if not ok then
    return
  end

  local chat_ok, chat = pcall(codecompanion.buf_get_chat, bufnr)
  if not chat_ok or not chat or not chat.ui or not is_valid_win(chat.ui.winnr) then
    return
  end

  if state.target_bufnr ~= bufnr then
    close_face_window()
  end

  state.target_bufnr = bufnr
  state.target_winnr = chat.ui.winnr
  state.tick = 0
  start_timer()
  render()
end

api.nvim_create_autocmd("User", {
  group = group,
  pattern = "CodeCompanionChatOpened",
  callback = function(args)
    if is_enabled() and args.data and args.data.bufnr then
      vim.schedule(function()
        set_target(args.data.bufnr)
      end)
    end
  end,
})

api.nvim_create_autocmd("User", {
  group = group,
  pattern = { "CodeCompanionChatHidden", "CodeCompanionChatClosed" },
  callback = function(args)
    if args.data and args.data.bufnr == state.target_bufnr then
      clear_target()
    end
  end,
})

api.nvim_create_autocmd("User", {
  group = group,
  pattern = "AIVoicePlaybackStarted",
  callback = function()
    state.talking = true
  end,
})

api.nvim_create_autocmd("User", {
  group = group,
  pattern = "AIVoicePlaybackStopped",
  callback = function()
    state.talking = false
  end,
})

api.nvim_create_autocmd({ "VimResized", "WinResized" }, {
  group = group,
  callback = function()
    if is_valid_win(state.target_winnr) then
      render()
    end
  end,
})

api.nvim_create_autocmd("VimLeavePre", {
  group = group,
  callback = function()
    clear_target()
  end,
})

create_or_replace_user_command("AIToggleFaceOverlay", function()
  set_enabled(not is_enabled())
end, {
  desc = "Toggle the AI face overlay panel",
})

create_or_replace_user_command("AIEnableFaceOverlay", function()
  set_enabled(true)
end, {
  desc = "Enable the AI face overlay panel",
})

create_or_replace_user_command("AIDisableFaceOverlay", function()
  set_enabled(false)
end, {
  desc = "Disable the AI face overlay panel",
})
