local M = {}
local uv = vim.uv or vim.loop
local namespace = vim.api.nvim_create_namespace("ai_voice_visualizer")
local sample_seconds = 0.01
local waveform_rate = 8000
-- Braille provides a 2 x 4 dot grid per terminal cell, rather than one block.
local dot_bits = { { 1, 2, 4, 64 }, { 8, 16, 32, 128 } }
local glyphs = {}
for mask = 1, 255 do
  glyphs[mask] = vim.fn.nr2char(0x2800 + mask)
end
local state

local function highlights()
  vim.api.nvim_set_hl(0, "NAIAVoiceNormal", { bg = "#081820", fg = "#36d9e8" })
  vim.api.nvim_set_hl(0, "NAIAVoiceBorder", { bg = "#081820", fg = "#287887" })
  vim.api.nvim_set_hl(0, "NAIAVoiceTitle", { bg = "#081820", fg = "#b8faff", bold = true })
  vim.api.nvim_set_hl(0, "NAIAVoiceBaseline", { fg = "#19434d" })
  vim.api.nvim_set_hl(0, "NAIAVoicePeak", { fg = "#b8faff" })
end

local function window_config()
  local width = math.min(36, vim.o.columns - 4)
  local height = 7
  local bottom = vim.o.lines - vim.o.cmdheight - (vim.o.laststatus == 0 and 0 or 1)
  if width < 12 or bottom < height + 3 then
    return nil
  end

  -- Offsets are terminal cells measured inward from the selected editor corner.
  -- Read on every frame so settings can also be adjusted during playback.
  local anchor = tostring(vim.g.ai_voice_visualizer_anchor or "SW"):upper()
  if not vim.tbl_contains({ "NW", "NE", "SW", "SE" }, anchor) then
    anchor = "SW"
  end
  local function offset(value, maximum)
    value = tonumber(value)
    if not value or value ~= value or math.abs(value) == math.huge then
      value = 1
    end
    return math.max(0, math.min(maximum, math.floor(value)))
  end
  local x = offset(vim.g.ai_voice_visualizer_offset_x, vim.o.columns - width - 2)
  local y = offset(vim.g.ai_voice_visualizer_offset_y, bottom - height - 2)
  return {
    relative = "editor",
    anchor = anchor,
    row = anchor:sub(1, 1) == "S" and bottom - y or y,
    col = anchor:sub(2, 2) == "E" and vim.o.columns - x or x,
    width = width,
    height = height,
    style = "minimal",
    border = "rounded",
    title = " N.A.I.A ",
    title_pos = "center",
    focusable = false,
    mouse = false,
    zindex = 45,
  }
end

function M.stop()
  local previous = state
  state = nil
  if not previous then
    return
  end
  previous.timer:stop()
  previous.timer:close()
  if previous.window and vim.api.nvim_win_is_valid(previous.window) then
    vim.api.nvim_win_close(previous.window, true)
  end
  if vim.api.nvim_buf_is_valid(previous.buffer) then
    vim.api.nvim_buf_delete(previous.buffer, { force = true })
  end
end

-- Decode the final playback file once, including any robot effects. Retain
-- signed PCM for a real oscilloscope trace, plus 10ms RMS levels for metering.
-- The caller owns cancellation and checks its request ID before using results.
function M.analyse(path, callback)
  if vim.g.ai_voice_visualizer_enabled == false or vim.fn.executable("ffmpeg") ~= 1 then
    vim.schedule(function()
      callback(nil)
    end)
    return nil
  end
  return vim.system({
    "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
    "-i", path, "-vn", "-ac", "1", "-ar", tostring(waveform_rate),
    "-c:a", "pcm_s16le", "-f", "s16le", "pipe:1",
  }, { text = false }, function(result)
    vim.schedule(function()
      local samples = {}
      local waveform = {}
      if result.code == 0 then
        local pcm = result.stdout or ""
        local sum, count = 0, 0
        for byte = 1, #pcm - 1, 2 do
          local low, high = pcm:byte(byte, byte + 1)
          local value = low + high * 256
          if value >= 32768 then
            value = value - 65536
          end
          value = value / 32768
          waveform[#waveform + 1] = value
          sum, count = sum + value * value, count + 1
          if count == waveform_rate * sample_seconds then
            samples[#samples + 1] = math.sqrt(sum / count)
            sum, count = 0, 0
          end
        end
        if count > 0 then
          samples[#samples + 1] = math.sqrt(sum / count)
        end
      end
      samples.waveform = waveform
      -- Analysis is optional: a failure must never prevent speech playback.
      callback(#samples > 0 and samples or nil)
    end)
  end)
end

local function render(current)
  if state ~= current then
    return
  end
  local config = window_config()
  if not config then
    if current.window and vim.api.nvim_win_is_valid(current.window) then
      vim.api.nvim_win_close(current.window, true)
      current.window = nil
    end
    return
  end
  if not vim.api.nvim_buf_is_valid(current.buffer) then
    M.stop()
    return
  end
  if not current.window or not vim.api.nvim_win_is_valid(current.window) then
    current.window = vim.api.nvim_open_win(current.buffer, false, config)
    vim.wo[current.window].winhighlight = "Normal:NAIAVoiceNormal,FloatBorder:NAIAVoiceBorder,FloatTitle:NAIAVoiceTitle"
    vim.wo[current.window].wrap = false
  elseif current.width ~= config.width or current.row ~= config.row or current.col ~= config.col
      or current.anchor ~= config.anchor then
    vim.api.nvim_win_set_config(current.window, config)
  end
  current.width, current.row, current.col = config.width, config.row, config.col
  current.anchor = config.anchor

  -- Positive delay holds the picture back to compensate for player/device startup.
  local delay = (tonumber(vim.g.ai_voice_visualizer_delay_ms) or 0) / 1000
  local elapsed = (uv.hrtime() - current.started) / 1e9 - delay
  local waveform = current.samples.waveform or {}
  local span = math.floor(waveform_rate * 0.02)
  local position = math.floor(elapsed * waveform_rate) - span + 1
  -- Trigger on a rising zero crossing: cycles stay anchored horizontally
  -- instead of drifting left like a scrolling recording. Only use past audio.
  if position > 1 and position + span <= #waveform then
    for at = position, math.max(2, position - math.floor(waveform_rate * 0.01)), -1 do
      if waveform[at - 1] <= 0 and waveform[at] > 0 then
        position = at
        break
      end
    end
  end
  local function sample_at(at)
    if elapsed < 0 or at < 1 or at > #waveform then
      return 0
    end
    local index = math.floor(at)
    local fraction = at - index
    local first = waveform[index] or 0
    return first + ((waveform[index + 1] or 0) - first) * fraction
  end
  local rows, bytes, marks = {}, {}, {}
  for row = 1, 7 do
    rows[row] = {}
    bytes[row] = 0
  end
  local previous_pixel
  for col = 1, config.width do
    local masks, tips = { 0, 0, 0, 0, 0, 0, 0 }, {}
    for subcol = 1, 2 do
      local dot = (col - 1) * 2 + subcol - 1
      local sample = sample_at(position + dot * (span - 1) / (config.width * 2 - 1))
      local amplitude = math.max(-1, math.min(1, sample * current.volume * 2.2))
      local pixel_y = math.floor(13.5 - amplitude * 12.5 + 0.5)
      -- Join successive points with a thin stroke, never a filled silhouette.
      for pixel = math.min(previous_pixel or pixel_y, pixel_y), math.max(previous_pixel or pixel_y, pixel_y) do
        local row = math.floor(pixel / 4) + 1
        masks[row] = masks[row] + dot_bits[subcol][pixel % 4 + 1]
        if math.abs(amplitude) > 0.65 then
          tips[row] = true
        end
      end
      previous_pixel = pixel_y
    end
    for row = 1, 7 do
      local char = glyphs[masks[row]] or " "
      local byte_start = bytes[row]
      if masks[row] > 0 then
        marks[#marks + 1] = {
          row - 1, byte_start, byte_start + #char,
          tips[row] and "NAIAVoicePeak" or (row == 4 and "NAIAVoiceBaseline" or "NAIAVoiceNormal"),
        }
      end
      rows[row][col] = char
      bytes[row] = byte_start + #char
    end
  end
  local lines = {}
  for row = 1, 7 do
    lines[row] = table.concat(rows[row])
  end
  vim.bo[current.buffer].modifiable = true
  vim.api.nvim_buf_set_lines(current.buffer, 0, -1, false, lines)
  vim.bo[current.buffer].modifiable = false
  vim.api.nvim_buf_clear_namespace(current.buffer, namespace, 0, -1)
  vim.api.nvim_buf_set_extmark(current.buffer, namespace, 3, 0, {
    end_col = #lines[4], hl_group = "NAIAVoiceBaseline", priority = 100,
  })
  for _, mark in ipairs(marks) do
    vim.api.nvim_buf_set_extmark(current.buffer, namespace, mark[1], mark[2], {
      end_col = mark[3], hl_group = mark[4], priority = 110,
    })
  end
end

function M.start(samples, volume)
  M.stop()
  if not samples or vim.g.ai_voice_visualizer_enabled == false then
    return
  end
  highlights()
  local buffer = vim.api.nvim_create_buf(false, true)
  vim.bo[buffer].bufhidden = "hide"
  vim.bo[buffer].swapfile = false
  local current = {
    samples = samples,
    volume = math.max(0, math.min(1, volume or 1)),
    started = uv.hrtime(),
    buffer = buffer,
    timer = assert(uv.new_timer()),
  }
  state = current
  current.timer:start(0, 33, vim.schedule_wrap(function()
    local ok, err = pcall(render, current)
    if not ok then
      M.stop()
      vim.notify("AI voice visualiser stopped: " .. tostring(err), vim.log.levels.WARN)
    end
  end))
end

local group = vim.api.nvim_create_augroup("NAIAVoiceVisualizer", { clear = true })
vim.api.nvim_create_autocmd("ColorScheme", { group = group, callback = highlights })
vim.api.nvim_create_autocmd("VimLeavePre", { group = group, callback = M.stop })

return M
