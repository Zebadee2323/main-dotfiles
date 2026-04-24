local config = vim.tbl_deep_extend("force", {
  start_delay_ms = 350,
  radius = 100,
  frames = 20,
  frame_delay_ms = 33,
  watch_debounce_ms = 80,
  trigger_on_save = true,
  trigger_on_external_change = false,
  trigger_on_ai_report = false,
  charset = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789!@#$%^&*()-_=+[]{};:,.<>/?\\|",
  sound_enabled = true,
  sound_volume = 1.0,
}, vim.g.file_change_fx or {})

_G.__file_change_fx_state = _G.__file_change_fx_state or {}
local persisted = _G.__file_change_fx_state
persisted.generation = (persisted.generation or 0) + 1

local generation = persisted.generation
local ns = vim.api.nvim_create_namespace("file_change_fx")
local group = vim.api.nvim_create_augroup("file_change_fx", { clear = true })
local uv = vim.uv or vim.loop
local snapshots = {}
local active_effects = {}
local watchers = {}
local pending_reload = {}
local pending_post_effect_opts = {}
local pending_save_snapshots = {}
local recent_manual_writes = {}
local guarded_pre_reload = {}
local intentional_reload = {}
local follow_debug_log = {}
local restore_autoread
local fx_audio_dir = vim.fn.stdpath("cache") .. "/file-change-fx"
local change_fx_sound_path = vim.fs.joinpath(vim.fn.stdpath("config"), "after", "plugin", "change-fx.mp3")
local current_sound_render_job = nil
local current_sound_play_job = nil
local current_sound_request_id = 0
local has_warned_missing_ffmpeg = false
local has_warned_missing_afplay = false
local has_warned_missing_source_sound = false
local cached_source_duration_s = nil

if persisted.watchers then
  for _, state in pairs(persisted.watchers) do
    pcall(function()
      if state.handle and not state.handle:is_closing() then
        state.handle:stop()
        state.handle:close()
      end
    end)
  end
end

for _, bufnr in ipairs(vim.api.nvim_list_bufs()) do
  if vim.api.nvim_buf_is_valid(bufnr) then
    pcall(vim.api.nvim_buf_clear_namespace, bufnr, ns, 0, -1)
  end
end

persisted.watchers = watchers
persisted.ns = ns
persisted.group = group
if persisted.enabled == nil then
  persisted.enabled = vim.g.file_change_fx_enabled ~= false
end
if persisted.trigger_on_save == nil then
  persisted.trigger_on_save = config.trigger_on_save ~= false
end
if persisted.trigger_on_external_change == nil then
  persisted.trigger_on_external_change = config.trigger_on_external_change == true
end
if persisted.trigger_on_ai_report == nil then
  persisted.trigger_on_ai_report = config.trigger_on_ai_report == true
end

vim.opt.autoread = true

local function set_transparent_fx_highlights()
  local function make_hl(name, spec)
    spec.bg = "NONE"
    spec.ctermbg = "NONE"
    vim.api.nvim_set_hl(0, name, spec)
  end

  make_hl("FileChangeFxAdd", { fg = "#66ff99", bold = true })
  make_hl("FileChangeFxAddNoise", { fg = "#2ee66b", bold = true, nocombine = true })

  make_hl("FileChangeFxMod", { fg = "#8fcbff", bold = true })
  make_hl("FileChangeFxModNoise", { fg = "#66b3ff", bold = true, nocombine = true })

  make_hl("FileChangeFxDel", { fg = "#ff6b6b", bold = true })
  make_hl("FileChangeFxDelNoise", { fg = "#ff3b3b", bold = true, nocombine = true })
end

set_transparent_fx_highlights()

local function is_current_generation()
  return _G.__file_change_fx_state and _G.__file_change_fx_state.generation == generation
end

local function is_fx_enabled()
  return _G.__file_change_fx_state and _G.__file_change_fx_state.enabled ~= false
end

local function is_save_trigger_enabled()
  return _G.__file_change_fx_state and _G.__file_change_fx_state.trigger_on_save ~= false
end

local function is_external_change_trigger_enabled()
  return _G.__file_change_fx_state and _G.__file_change_fx_state.trigger_on_external_change == true
end

local function is_ai_report_trigger_enabled()
  return _G.__file_change_fx_state and _G.__file_change_fx_state.trigger_on_ai_report == true
end

local function follow_debug_enabled()
  return _G.__file_change_fx_state and _G.__file_change_fx_state.follow_debug == true
end

local function normalize_path(path)
  if not path or path == "" then
    return nil
  end

  return vim.fs.normalize(path)
end

local function buf_lines(bufnr)
  return vim.api.nvim_buf_get_lines(bufnr, 0, -1, false)
end

local function join_lines(lines)
  return table.concat(lines, "\n")
end

local function split_chars(text)
  if text == "" then
    return {}
  end

  return vim.fn.split(text, [[\zs]])
end

local function collect_lines(target, data)
  if not data then
    return
  end

  for _, line in ipairs(data) do
    if line ~= "" then
      target[#target + 1] = line
    end
  end
end

local function push_follow_debug(message, level)
  local entry = string.format("[%s] %s", os.date("%H:%M:%S"), tostring(message))
  follow_debug_log[#follow_debug_log + 1] = entry
  if #follow_debug_log > 120 then
    table.remove(follow_debug_log, 1)
  end

  if follow_debug_enabled() then
    vim.schedule(function()
      vim.notify(entry, level or vim.log.levels.INFO, { title = "FileChangeFxDebug" })
    end)
  end
end

local function ensure_fx_audio_dir()
  vim.fn.mkdir(fx_audio_dir, "p")
end

local function is_sound_enabled()
  return is_fx_enabled() and config.sound_enabled ~= false
end

local function stop_sound_jobs()
  if current_sound_render_job and vim.fn.jobwait({ current_sound_render_job }, 0)[1] == -1 then
    vim.fn.jobstop(current_sound_render_job)
  end
  current_sound_render_job = nil

  if current_sound_play_job and vim.fn.jobwait({ current_sound_play_job }, 0)[1] == -1 then
    vim.fn.jobstop(current_sound_play_job)
  end
  current_sound_play_job = nil
end

local function stop_sound_playback()
  if current_sound_play_job and vim.fn.jobwait({ current_sound_play_job }, 0)[1] == -1 then
    vim.fn.jobstop(current_sound_play_job)
  end
  current_sound_play_job = nil
end

local function start_sound_playback(wav_path, request_id)
  local play_job = vim.fn.jobstart({ "afplay", "-v", tostring(config.sound_volume), wav_path }, {
    on_exit = function()
      if current_sound_request_id == request_id then
        current_sound_play_job = nil
      end
    end,
  })

  if play_job <= 0 then
    if not has_warned_missing_afplay then
      has_warned_missing_afplay = true
      vim.schedule(function()
        vim.notify("file-change-fx could not start afplay for glitch audio", vim.log.levels.WARN)
      end)
    end
    return
  end

  current_sound_play_job = play_job
end

local function change_fx_preview_path(request_id)
  return vim.fs.joinpath(fx_audio_dir, string.format("change-fx-preview-%d.wav", request_id))
end

local function source_sound_duration_s()
  if cached_source_duration_s then
    return cached_source_duration_s
  end

  if vim.fn.executable("ffprobe") ~= 1 or vim.fn.filereadable(change_fx_sound_path) ~= 1 then
    return nil
  end

  local output = vim.fn.systemlist({
    "ffprobe",
    "-v",
    "error",
    "-show_entries",
    "format=duration",
    "-of",
    "default=noprint_wrappers=1:nokey=1",
    change_fx_sound_path,
  })

  if vim.v.shell_error ~= 0 or not output or not output[1] then
    return nil
  end

  local duration_s = tonumber(output[1])
  if not duration_s or duration_s <= 0 then
    return nil
  end

  cached_source_duration_s = duration_s
  return duration_s
end

local function random_start_offset_s(duration_s, request_id)
  local source_duration_s_value = source_sound_duration_s()
  if not source_duration_s_value then
    return 0
  end

  local usable_duration_s = math.min(duration_s, source_duration_s_value)
  local max_start_s = math.max(source_duration_s_value - usable_duration_s, 0)
  if max_start_s <= 0 then
    return 0
  end

  local seed = tonumber(tostring(uv.hrtime()):sub(-9)) or request_id or 1
  return (seed % 1000000) / 1000000 * max_start_s
end

local function play_glitch_sound(duration_ms)
  if not is_sound_enabled() then
    return
  end

  if vim.fn.executable("ffmpeg") ~= 1 then
    if not has_warned_missing_ffmpeg then
      has_warned_missing_ffmpeg = true
      vim.schedule(function()
        vim.notify("file-change-fx sound requires ffmpeg", vim.log.levels.WARN)
      end)
    end
    return
  end

  if vim.fn.executable("afplay") ~= 1 then
    if not has_warned_missing_afplay then
      has_warned_missing_afplay = true
      vim.schedule(function()
        vim.notify("file-change-fx sound requires afplay", vim.log.levels.WARN)
      end)
    end
    return
  end

  if vim.fn.filereadable(change_fx_sound_path) ~= 1 then
    if not has_warned_missing_source_sound then
      has_warned_missing_source_sound = true
      vim.schedule(function()
        vim.notify("file-change-fx could not find change-fx.mp3", vim.log.levels.WARN)
      end)
    end
    return
  end

  duration_ms = math.max(math.floor(duration_ms or 0), 1)
  local duration_s = duration_ms / 1000
  current_sound_request_id = current_sound_request_id + 1
  local request_id = current_sound_request_id
  local wav_path = change_fx_preview_path(request_id)
  local start_offset_s = random_start_offset_s(duration_s, request_id)

  stop_sound_playback()
  ensure_fx_audio_dir()

  local stderr = {}
  local render_job = vim.fn.jobstart({
    "ffmpeg",
    "-y",
    "-ss",
    string.format("%.3f", start_offset_s),
    "-t",
    string.format("%.3f", duration_s),
    "-i",
    change_fx_sound_path,
    "-vn",
    "-acodec",
    "pcm_s16le",
    wav_path,
  }, {
    stderr_buffered = true,
    on_stderr = function(_, data)
      collect_lines(stderr, data)
    end,
    on_exit = function(_, code)
      vim.schedule(function()
        if current_sound_request_id ~= request_id then
          return
        end

        current_sound_render_job = nil

        if code ~= 0 then
          vim.notify(
            "file-change-fx glitch audio generation failed: "
              .. (#stderr > 0 and table.concat(stderr, "\n") or ("ffmpeg exited with code " .. code)),
            vim.log.levels.WARN
          )
          return
        end

        start_sound_playback(wav_path, request_id)
      end)
    end,
  })

  if render_job <= 0 then
    vim.schedule(function()
      vim.notify("file-change-fx could not start ffmpeg for glitch audio", vim.log.levels.WARN)
    end)
    return
  end

  current_sound_render_job = render_job
end

local function large_placeholder_method()
  local payload = {
    enabled = true,
    description = "Temporary helper for validating file-change-fx behavior.",
    tags = {
      "manual-save",
      "external-change",
    },
  }

  local summary = string.format("%s:%s", payload.variant, payload.description)
  local suffix = payload.enabled and ":ready" or ":idle"
  local message = summary .. suffix
  local attempts = payload.retries + #payload.tags

  return message, attempts, payload
end

local function current_tab_visible_file_bufs()
  local seen = {}
  local result = {}

  for _, win in ipairs(vim.api.nvim_tabpage_list_wins(0)) do
    if vim.api.nvim_win_is_valid(win) and vim.fn.win_gettype(win) == "" then
      local bufnr = vim.api.nvim_win_get_buf(win)
      if vim.api.nvim_buf_is_valid(bufnr) and vim.bo[bufnr].buftype == "" then
        local path = normalize_path(vim.api.nvim_buf_get_name(bufnr))
        if path and vim.fn.filereadable(path) == 1 and not seen[path] then
          seen[path] = bufnr
          result[#result + 1] = { bufnr = bufnr, path = path }
        end
      end
    end
  end

  return result
end

local function is_buf_visible_in_current_tab(bufnr)
  for _, win in ipairs(vim.api.nvim_tabpage_list_wins(0)) do
    if vim.api.nvim_win_is_valid(win)
      and vim.fn.win_gettype(win) == ""
      and vim.api.nvim_win_get_buf(win) == bufnr
    then
      return true
    end
  end

  return false
end

local function is_buf_visible_in_any_window(bufnr)
  if not vim.api.nvim_buf_is_valid(bufnr) then
    return false
  end

  return vim.fn.bufwinid(bufnr) ~= -1
end

local function get_target_win(bufnr)
  local current = vim.api.nvim_get_current_win()
  if vim.api.nvim_win_is_valid(current)
    and vim.fn.win_gettype(current) == ""
    and vim.api.nvim_win_get_buf(current) == bufnr
  then
    return current
  end

  for _, win in ipairs(vim.api.nvim_tabpage_list_wins(0)) do
    if vim.api.nvim_win_is_valid(win)
      and vim.fn.win_gettype(win) == ""
      and vim.api.nvim_win_get_buf(win) == bufnr
    then
      return win
    end
  end
end

local function is_real_disk_file_buffer(bufnr)
  if not vim.api.nvim_buf_is_valid(bufnr) or vim.bo[bufnr].buftype ~= "" then
    return false
  end

  local path = normalize_path(vim.api.nvim_buf_get_name(bufnr))
  return path ~= nil and vim.fn.filereadable(path) == 1
end

local function is_normal_file_edit_win(win)
  if not vim.api.nvim_win_is_valid(win) or vim.fn.win_gettype(win) ~= "" then
    return false
  end

  local bufnr = vim.api.nvim_win_get_buf(win)
  return is_real_disk_file_buffer(bufnr)
end

local function is_probably_text_file(path)
  local fd = uv.fs_open(path, "r", 438)
  if not fd then
    return false
  end

  local chunk = uv.fs_read(fd, 1024, 0)
  uv.fs_close(fd)

  if chunk == nil then
    return false
  end

  return not chunk:find("\0", 1, true)
end

local function best_editing_win()
  local best_win
  local best_area = -1

  for _, win in ipairs(vim.api.nvim_tabpage_list_wins(0)) do
    if is_normal_file_edit_win(win) then
      local width = vim.api.nvim_win_get_width(win)
      local height = vim.api.nvim_win_get_height(win)
      local area = width * height
      if area > best_area then
        best_area = area
        best_win = win
      end
    end
  end

  local current = vim.api.nvim_get_current_win()
  if not best_win and is_normal_file_edit_win(current) then
    best_win = current
  end

  return best_win
end

local function refresh_buffer_highlighting(bufnr)
  vim.schedule(function()
    if not vim.api.nvim_buf_is_valid(bufnr) then
      return
    end

    local filetype = vim.bo[bufnr].filetype
    if filetype and filetype ~= "" then
      pcall(function()
        local parser = vim.treesitter.get_parser(bufnr, filetype)
        if parser then
          parser:parse(true)
        end
      end)
    end

    local win = get_target_win(bufnr)
    if win and vim.api.nvim_win_is_valid(win) then
      pcall(vim.api.nvim_win_call, win, function()
        vim.cmd("redraw!")
      end)
    end
  end)
end

local function center_on_line(bufnr, line)
  local win = get_target_win(bufnr)
  if not win then
    return
  end

  line = math.max(1, line)
  local max_line = math.max(vim.api.nvim_buf_line_count(bufnr), 1)
  line = math.min(line, max_line)

  vim.api.nvim_win_call(win, function()
    vim.api.nvim_win_set_cursor(win, { line, 0 })
    vim.cmd("normal! zz")
  end)
end

local function get_visible_line_range(bufnr)
  local win = get_target_win(bufnr)
  if not (win and vim.api.nvim_win_is_valid(win)) then
    return nil
  end

  local info = vim.fn.getwininfo(win)[1]
  if not info then
    return nil
  end

  local start_line = math.max(info.topline or 1, 1)
  local end_line = math.max(info.botline or start_line, start_line)
  local line_count = math.max(vim.api.nvim_buf_line_count(bufnr), 1)

  return {
    start_line = start_line,
    end_line = math.min(end_line, line_count),
  }
end

local function choose_hunk_center(hunks)
  local best_hunk
  local best_score = -1

  for _, hunk in ipairs(hunks) do
    local old_count = hunk[2]
    local new_count = hunk[4]
    local span = math.max(old_count, new_count, 1)
    local score = span * 1000 + math.min(old_count, new_count)
    if score > best_score then
      best_score = score
      best_hunk = hunk
    end
  end

  if not best_hunk then
    return 1
  end

  local start_new = best_hunk[3]
  local new_count = best_hunk[4]
  local effective_count = math.max(new_count, 1)
  return math.max(1, start_new + math.floor((effective_count - 1) / 2))
end

local function choose_old_hunk_center(hunks)
  local best_hunk
  local best_score = -1

  for _, hunk in ipairs(hunks) do
    local old_count = hunk[2]
    local new_count = hunk[4]
    local span = math.max(old_count, new_count, 1)
    local score = span * 1000 + math.min(old_count, new_count)
    if score > best_score then
      best_score = score
      best_hunk = hunk
    end
  end

  if not best_hunk then
    return 1
  end

  local start_old = best_hunk[1]
  local old_count = best_hunk[2]
  local effective_count = math.max(old_count, 1)
  return math.max(1, start_old + math.floor((effective_count - 1) / 2))
end

local function build_window_state(old_lines, new_lines, hunks, center_line, opts)
  opts = opts or {}
  local start_line
  local end_line
  if opts.start_line and opts.end_line then
    start_line = math.max(1, opts.start_line)
    end_line = math.min(#new_lines, math.max(opts.end_line, start_line))
  else
    start_line = math.max(1, center_line - config.radius)
    end_line = math.min(#new_lines, center_line + config.radius)
  end
  local changed_new = {}
  local old_by_new = {}
  local kind_by_new = {}
  local deleted_lines_by_new = {}
  local deleted_lines_above_by_new = {}

  local old_index = 1
  local new_index = 1

  for _, hunk in ipairs(hunks) do
    local old_start, old_count, new_start, new_count = unpack(hunk)

    while new_index < new_start do
      if new_index >= start_line and new_index <= end_line then
        old_by_new[new_index] = old_lines[old_index] or new_lines[new_index] or ""
      end
      old_index = old_index + 1
      new_index = new_index + 1
    end

    if new_count == 0 and old_count > 0 then
      local anchor_new = math.min(math.max(new_start, 1), math.max(#new_lines, 1))
      if opts.include_delete_states ~= false and anchor_new >= start_line and anchor_new <= end_line then
        local deleted_preview = {}
        local preview_count = math.min(old_count, 3)
        for k = 0, preview_count - 1 do
          deleted_preview[#deleted_preview + 1] = old_lines[old_start + k] or ""
        end
        if old_count > preview_count then
          deleted_preview[#deleted_preview + 1] = "…"
        end
        old_by_new[anchor_new] = new_lines[anchor_new] or ""
        changed_new[anchor_new] = true
        kind_by_new[anchor_new] = "del"
        deleted_lines_by_new[anchor_new] = deleted_preview
        deleted_lines_above_by_new[anchor_new] = new_start <= #new_lines
      end
    end

    for j = 0, new_count - 1 do
      local new_lnum = new_start + j
      if new_lnum >= start_line and new_lnum <= end_line then
        local mapped_old
        if old_count == 0 then
          mapped_old = ""
        else
          local old_offset = math.floor((j * old_count) / math.max(new_count, 1))
          mapped_old = old_lines[old_start + old_offset] or ""
        end
        old_by_new[new_lnum] = mapped_old
        changed_new[new_lnum] = true
        if old_count == 0 then
          kind_by_new[new_lnum] = "add"
        else
          kind_by_new[new_lnum] = kind_by_new[new_lnum] or "mod"
        end
      end
    end

    old_index = old_start + old_count
    new_index = new_start + new_count
  end

  while new_index <= #new_lines do
    if new_index >= start_line and new_index <= end_line then
      old_by_new[new_index] = old_lines[old_index] or new_lines[new_index] or ""
    end
    old_index = old_index + 1
    new_index = new_index + 1
  end

  local line_states = {}
  for lnum = start_line, end_line do
    if changed_new[lnum] then
      local old_text = old_by_new[lnum] or ""
      local new_text = new_lines[lnum] or ""
      local kind = kind_by_new[lnum] or "mod"
      line_states[lnum] = {
        old_chars = split_chars(old_text),
        new_chars = split_chars(new_text),
        kind = kind,
        deleted_lines = deleted_lines_by_new[lnum],
        deleted_lines_above = deleted_lines_above_by_new[lnum],
      }
    end
  end

  return {
    line_states = line_states,
    center_line = center_line,
  }
end

local function build_pre_delete_state(old_lines, new_lines, hunks, center_line, opts)
  opts = opts or {}
  local start_line
  local end_line
  if opts.start_line and opts.end_line then
    start_line = math.max(1, opts.start_line)
    end_line = math.min(#old_lines, math.max(opts.end_line, start_line))
  else
    start_line = math.max(1, center_line - config.radius)
    end_line = math.min(#old_lines, center_line + config.radius)
  end
  local line_states = {}

  for _, hunk in ipairs(hunks) do
    local old_start, old_count, _, new_count = unpack(hunk)
    local deleted_count = math.max(old_count - new_count, 0)
    if deleted_count > 0 then
      local delete_start = old_start + math.min(new_count, old_count)
      for offset = 0, deleted_count - 1 do
        local lnum = delete_start + offset
        if lnum >= start_line and lnum <= end_line then
          local text = old_lines[lnum] or ""
          line_states[lnum] = {
            old_chars = split_chars(text),
            new_chars = {},
            kind = "del",
          }
        end
      end
    end
  end

  return {
    line_states = line_states,
    center_line = center_line,
  }
end

local function noise_char(seed)
  local index = (seed % #config.charset) + 1
  return config.charset:sub(index, index)
end

local function morph_line(state, progress, frame, lnum)
  local old_chars = state.old_chars
  local new_chars = state.new_chars
  local max_len = math.max(#old_chars, #new_chars)
  local out = {}

  for i = 1, max_len do
    local old_ch = old_chars[i] or " "
    local new_ch = new_chars[i] or " "

    if old_ch == new_ch then
      out[i] = new_ch
    elseif progress < 0.35 then
      if progress >= (i / math.max(max_len, 1)) * 0.22 then
        out[i] = noise_char((frame * 31) + (lnum * 17) + i)
      else
        out[i] = old_ch
      end
    elseif progress < 0.75 then
      local settle_at = 0.35 + (i / math.max(max_len, 1)) * 0.40
      if progress >= settle_at then
        out[i] = new_ch
      else
        out[i] = noise_char((frame * 47) + (lnum * 13) + i)
      end
    else
      out[i] = new_ch
    end
  end

  return table.concat(out):gsub("%s+$", "")
end

local function clear_effect(bufnr)
  local effect = active_effects[bufnr]

  if vim.api.nvim_buf_is_valid(bufnr) then
    pcall(vim.api.nvim_buf_clear_namespace, bufnr, ns, 0, -1)

    if effect and effect.restore_modifiable ~= nil then
      vim.bo[bufnr].modifiable = effect.restore_modifiable
    end
  end

  active_effects[bufnr] = nil
  refresh_buffer_highlighting(bufnr)
end

local function disable_all_effects()
  for bufnr in pairs(active_effects) do
    clear_effect(bufnr)
  end

  stop_sound_playback()

  for bufnr in pairs(guarded_pre_reload) do
    restore_autoread(bufnr)
  end

  snapshots = {}
  pending_reload = {}
  pending_post_effect_opts = {}
  guarded_pre_reload = {}
  intentional_reload = {}
end

restore_autoread = function(bufnr)
  local guard = guarded_pre_reload[bufnr]
  if not guard then
    return
  end

  guarded_pre_reload[bufnr] = nil

  if vim.api.nvim_buf_is_valid(bufnr) then
    vim.bo[bufnr].autoread = guard.autoread
  end
end

local function reload_buffer_preserving_view(bufnr)
  local win = get_target_win(bufnr)
  if not (win and vim.api.nvim_win_is_valid(win)) then
    return false
  end

  local view = vim.api.nvim_win_call(win, function()
    return vim.fn.winsaveview()
  end)

  local reloaded = pcall(vim.api.nvim_win_call, win, function()
    vim.cmd("silent! edit!")
  end)

  if not reloaded then
    return false
  end

  pcall(vim.api.nvim_win_call, win, function()
    vim.fn.winrestview(view)
  end)

  return true
end

local function create_follow_window(bufnr)
  local anchor = best_editing_win() or vim.api.nvim_get_current_win()
  if not vim.api.nvim_win_is_valid(anchor) then
    return nil
  end

  local new_win
  local ok = pcall(vim.api.nvim_win_call, anchor, function()
    vim.cmd("leftabove vsplit")
    new_win = vim.api.nvim_get_current_win()
  end)

  if not ok or not (new_win and vim.api.nvim_win_is_valid(new_win)) then
    return nil
  end

  if not pcall(vim.api.nvim_win_set_buf, new_win, bufnr) then
    return nil
  end

  return new_win
end

local function open_buffer_in_best_window(bufnr)
  local existing = get_target_win(bufnr)
  if existing and vim.api.nvim_win_is_valid(existing) then
    return existing
  end

  local win = best_editing_win()
  if win and pcall(vim.api.nvim_win_set_buf, win, bufnr) then
    return win
  end

  return create_follow_window(bufnr)
end

local function normalize_reported_hunks(hunks)
  if type(hunks) ~= "table" then
    return nil
  end

  local normalized = {}

  for _, hunk in ipairs(hunks) do
    if type(hunk) == "table" then
      local kind = hunk.kind or hunk.type
      local start_line = tonumber(hunk.start_line or hunk.line_start or hunk.start or hunk.from_line)
      local end_line = tonumber(hunk.end_line or hunk.line_end or hunk["end"] or hunk.to_line)

      if type(kind) == "string" then
        kind = vim.trim(kind):lower()
      end

      if kind == "addition" then
        kind = "add"
      elseif kind == "modification" or kind == "modify" or kind == "change" then
        kind = "mod"
      elseif kind == "deletion" or kind == "remove" then
        kind = "del"
      end

      if (kind == "add" or kind == "mod" or kind == "del") and start_line and end_line then
        start_line = math.max(1, math.floor(start_line))
        end_line = math.max(start_line, math.floor(end_line))
        normalized[#normalized + 1] = {
          kind = kind,
          start_line = start_line,
          end_line = end_line,
        }
      end
    end
  end

  if #normalized == 0 then
    return nil
  end

  table.sort(normalized, function(a, b)
    if a.start_line == b.start_line then
      return a.end_line < b.end_line
    end
    return a.start_line < b.start_line
  end)

  return normalized
end

local function choose_reported_hunk_center(hunks)
  local best_hunk
  local best_span = -1

  for _, hunk in ipairs(hunks) do
    local span = math.max(hunk.end_line - hunk.start_line + 1, 1)
    if span > best_span then
      best_span = span
      best_hunk = hunk
    end
  end

  if not best_hunk then
    return 1
  end

  return math.max(1, best_hunk.start_line + math.floor((best_hunk.end_line - best_hunk.start_line) / 2))
end

local function build_reported_window_state(bufnr, hunks, center_line, opts)
  opts = opts or {}
  local new_lines = buf_lines(bufnr)
  local max_line = math.max(#new_lines, 1)
  local start_line
  local end_line

  if opts.start_line and opts.end_line then
    start_line = math.max(1, opts.start_line)
    end_line = math.max(start_line, opts.end_line)
  else
    start_line = math.max(1, center_line - config.radius)
    end_line = center_line + config.radius
  end

  local line_states = {}

  for _, hunk in ipairs(hunks) do
    if hunk.kind == "del" then
      local anchor_line = math.min(math.max(hunk.start_line, 1), max_line)
      if anchor_line >= start_line and anchor_line <= end_line then
        local deleted_lines = {}
        local deleted_count = math.max(hunk.end_line - hunk.start_line + 1, 1)
        for index = 1, deleted_count do
          deleted_lines[#deleted_lines + 1] = string.rep(" ", 12)
        end
        line_states[anchor_line] = {
          old_chars = {},
          new_chars = split_chars(new_lines[anchor_line] or ""),
          kind = "del",
          deleted_lines = deleted_lines,
          deleted_lines_above = hunk.start_line <= #new_lines,
        }
      end
    else
      local hunk_end = math.min(hunk.end_line, max_line)
      for lnum = hunk.start_line, hunk_end do
        if lnum >= start_line and lnum <= end_line then
          local text = new_lines[lnum] or ""
          local synthetic_old = hunk.kind == "add" and "" or string.rep(" ", vim.fn.strchars(text))
          line_states[lnum] = {
            old_chars = split_chars(synthetic_old),
            new_chars = split_chars(text),
            kind = hunk.kind,
          }
        end
      end
    end
  end

  return {
    line_states = line_states,
    center_line = center_line,
  }
end

local render_frame

local function start_prebuilt_effect(bufnr, effect, opts)
  opts = opts or {}
  if not is_fx_enabled() then
    return false
  end
  if not is_current_generation() or not vim.api.nvim_buf_is_valid(bufnr) or not is_buf_visible_in_current_tab(bufnr) then
    return false
  end
  if not effect or not next(effect.line_states or {}) then
    return false
  end

  if opts.center ~= false then
    center_on_line(bufnr, effect.center_line or 1)
  end

  local previous_effect = active_effects[bufnr]
  if previous_effect then
    clear_effect(bufnr)
  end

  local id = (previous_effect and previous_effect.id or 0) + 1
  effect.id = id
  effect.frames = math.max(config.frames, 1)
  effect.frame_delay_ms = math.max(config.frame_delay_ms, 1)
  effect.restore_modifiable = vim.bo[bufnr].modifiable
  vim.bo[bufnr].modifiable = false
  active_effects[bufnr] = effect

  vim.defer_fn(function()
    if not is_current_generation() then
      return
    end

    local current = active_effects[bufnr]
    if not current or current.id ~= id then
      return
    end

    play_glitch_sound(effect.frames * effect.frame_delay_ms)
    render_frame(bufnr, effect, 1)
  end, math.max(opts.start_delay_ms ~= nil and opts.start_delay_ms or config.start_delay_ms, 0))

  return true
end

local function load_buffer_for_reported_follow(path)
  if vim.fn.filereadable(path) ~= 1 then
    push_follow_debug("skipping reported follow for missing file: " .. tostring(path), vim.log.levels.WARN)
    return nil, nil
  end

  local bufnr = vim.fn.bufnr(path)

  if bufnr > 0 and vim.api.nvim_buf_is_valid(bufnr) then
    if vim.bo[bufnr].buftype ~= "" then
      push_follow_debug("existing buffer is not a normal file buffer: " .. tostring(path), vim.log.levels.WARN)
      return nil, nil
    end

    if vim.bo[bufnr].modified then
      push_follow_debug("skipping modified buffer for reported follow: " .. tostring(path), vim.log.levels.WARN)
      return nil, nil
    end
  else
    bufnr = vim.fn.bufadd(path)
    if bufnr <= 0 then
      return nil, nil
    end
  end

  vim.fn.bufload(bufnr)

  local win = open_buffer_in_best_window(bufnr)
  if not win then
    return nil, nil
  end

  reload_buffer_preserving_view(bufnr)

  return bufnr, win
end

local function play_reported_follow_change(path, hunks)
  push_follow_debug("handling reported follow change for " .. tostring(path))

  local center_line = choose_reported_hunk_center(hunks)
  local bufnr, win = load_buffer_for_reported_follow(path)
  if not bufnr then
    push_follow_debug("could not load/open buffer for reported change: " .. tostring(path), vim.log.levels.WARN)
    return false, "Could not load buffer for " .. tostring(path)
  end

  if win and vim.api.nvim_win_is_valid(win) then
    center_on_line(bufnr, center_line)
  end

  local effect = build_reported_window_state(bufnr, hunks, center_line)

  if not next(effect.line_states or {}) then
    push_follow_debug("reported hunks did not map to visible lines for " .. tostring(path), vim.log.levels.WARN)
    return false, "No valid line states for " .. tostring(path)
  end

  if not start_prebuilt_effect(bufnr, effect, {
    start_delay_ms = 0,
    center = true,
  }) then
    return false, "Could not start effect for " .. tostring(path)
  end

  return true
end

local function should_show_fx_overlay(progress, frame, lnum)
  if progress >= 0.82 then
    return true
  end

  local pulse = (frame + lnum) % 2 == 0
  if progress < 0.28 then
    return pulse
  elseif progress < 0.55 then
    return not pulse
  else
    return true
  end
end

local function highlight_for_state(kind, progress)
  local noisy = progress < 0.75
  if kind == "add" then
    return noisy and "FileChangeFxAddNoise" or "FileChangeFxAdd"
  elseif kind == "del" then
    return noisy and "FileChangeFxDelNoise" or "FileChangeFxDel"
  else
    return noisy and "FileChangeFxModNoise" or "FileChangeFxMod"
  end
end

render_frame = function(bufnr, effect, frame)
  if not is_current_generation() or not vim.api.nvim_buf_is_valid(bufnr) or not is_buf_visible_in_current_tab(bufnr) then
    clear_effect(bufnr)
    return
  end

  vim.api.nvim_buf_clear_namespace(bufnr, ns, 0, -1)
  local progress = frame / effect.frames

  for lnum, state in pairs(effect.line_states) do
    local hl = highlight_for_state(state.kind, progress)
    local show_fx = state.kind == "del" and state.deleted_lines and #state.deleted_lines > 0
      or should_show_fx_overlay(progress, frame, lnum)

    if show_fx then
      if state.kind == "del" and state.deleted_lines and #state.deleted_lines > 0 then
        local virt_lines = {}

        for index, deleted_text in ipairs(state.deleted_lines) do
          local deleted_state = {
            old_chars = split_chars(deleted_text),
            new_chars = {},
          }
          local fake_line = morph_line(deleted_state, progress, frame, lnum + index - 1)
          virt_lines[#virt_lines + 1] = {
            { (fake_line ~= "" and fake_line or " ") .. " ", hl },
          }
        end

        vim.api.nvim_buf_set_extmark(bufnr, ns, lnum - 1, 0, {
          end_line = lnum,
          hl_group = hl,
          hl_eol = true,
          priority = 10000,
          virt_lines = virt_lines,
          virt_lines_above = state.deleted_lines_above == true,
        })
      else
        local text = morph_line(state, progress, frame, lnum)
        vim.api.nvim_buf_set_extmark(bufnr, ns, lnum - 1, 0, {
          end_line = lnum,
          hl_group = hl,
          hl_eol = true,
          priority = 10000,
          virt_text = text ~= "" and {
            { text .. " ", hl },
          } or nil,
          virt_text_pos = text ~= "" and "overlay" or nil,
        })
      end
    end
  end

  if frame >= effect.frames then
    local on_complete = effect.on_complete
    if type(on_complete) == "function" then
      on_complete()
      local current = active_effects[bufnr]
      if current and current.id == effect.id then
        clear_effect(bufnr)
      end
    else
      clear_effect(bufnr)
    end
    return
  end

  vim.defer_fn(function()
    if not is_current_generation() then
      return
    end

    local current = active_effects[bufnr]
    if not current or current.id ~= effect.id then
      return
    end

    render_frame(bufnr, effect, frame + 1)
  end, effect.frame_delay_ms)
end

local function start_effect(bufnr, old_lines, opts)
  opts = opts or {}
  if not is_fx_enabled() then
    return
  end
  if not is_current_generation() or not vim.api.nvim_buf_is_valid(bufnr) or not is_buf_visible_in_current_tab(bufnr) then
    return
  end

  local new_lines = buf_lines(bufnr)
  if vim.deep_equal(old_lines, new_lines) then
    return
  end

  local hunks = vim.diff(join_lines(old_lines), join_lines(new_lines), {
    result_type = "indices",
    algorithm = "histogram",
  }) or {}

  if #hunks == 0 then
    return
  end

  local center_line = choose_hunk_center(hunks)
  local visible_range = opts.visible_only and get_visible_line_range(bufnr) or nil
  local effect = build_window_state(old_lines, new_lines, hunks, center_line, {
    include_delete_states = opts.include_delete_states,
    start_line = visible_range and visible_range.start_line or nil,
    end_line = visible_range and visible_range.end_line or nil,
  })
  if not next(effect.line_states) then
    return
  end

  if opts.center ~= false then
    center_on_line(bufnr, center_line)
  end

  local previous_effect = active_effects[bufnr]
  if previous_effect then
    clear_effect(bufnr)
  end

  local id = (previous_effect and previous_effect.id or 0) + 1
  effect.id = id
  effect.frames = math.max(config.frames, 1)
  effect.frame_delay_ms = math.max(config.frame_delay_ms, 1)
  effect.restore_modifiable = vim.bo[bufnr].modifiable
  vim.bo[bufnr].modifiable = false
  active_effects[bufnr] = effect

  vim.defer_fn(function()
    if not is_current_generation() then
      return
    end

    local current = active_effects[bufnr]
    if not current or current.id ~= id then
      return
    end

    play_glitch_sound(effect.frames * effect.frame_delay_ms)
    render_frame(bufnr, effect, 1)
  end, math.max(opts.start_delay_ms ~= nil and opts.start_delay_ms or config.start_delay_ms, 0))
end

local function start_pre_delete_effect(bufnr, old_lines, new_lines, path, hunks, opts)
  opts = opts or {}
  if not is_fx_enabled() then
    return false
  end
  if not is_current_generation() or not vim.api.nvim_buf_is_valid(bufnr) or not is_buf_visible_in_current_tab(bufnr) then
    return false
  end

  local center_line = choose_old_hunk_center(hunks)
  local visible_range = opts.visible_only and get_visible_line_range(bufnr) or nil
  local effect = build_pre_delete_state(old_lines, new_lines, hunks, center_line, {
    start_line = visible_range and visible_range.start_line or nil,
    end_line = visible_range and visible_range.end_line or nil,
  })
  if not next(effect.line_states) then
    return false
  end

  if opts.center ~= false then
    center_on_line(bufnr, center_line)
  end

  local previous_effect = active_effects[bufnr]
  if previous_effect then
    clear_effect(bufnr)
  end

  local id = (previous_effect and previous_effect.id or 0) + 1
  effect.id = id
  effect.frames = math.max(config.frames, 1)
  effect.frame_delay_ms = math.max(config.frame_delay_ms, 1)
  effect.restore_modifiable = vim.bo[bufnr].modifiable
  guarded_pre_reload[bufnr] = {
    autoread = vim.bo[bufnr].autoread,
  }
  vim.bo[bufnr].autoread = false
  effect.on_complete = function()
    if not is_current_generation()
      or not vim.api.nvim_buf_is_valid(bufnr)
      or not is_buf_visible_in_current_tab(bufnr)
    then
      restore_autoread(bufnr)
      return
    end

    local current_path = normalize_path(vim.api.nvim_buf_get_name(bufnr))
    if current_path ~= path or vim.fn.filereadable(path) ~= 1 then
      restore_autoread(bufnr)
      return
    end

    restore_autoread(bufnr)

    local reloaded = reload_buffer_preserving_view(bufnr)

    if not reloaded then
      snapshots[bufnr] = old_lines
      pending_post_effect_opts[bufnr] = {
        start_delay_ms = 0,
        include_delete_states = false,
        visible_only = opts.visible_only,
        center = opts.center,
      }
      intentional_reload[bufnr] = true
      vim.cmd(string.format("silent! checktime %d", bufnr))
      return
    end

    vim.schedule(function()
      if not is_current_generation()
        or not vim.api.nvim_buf_is_valid(bufnr)
        or not is_buf_visible_in_current_tab(bufnr)
      then
        return
      end

      start_effect(bufnr, old_lines, {
        start_delay_ms = 0,
        include_delete_states = false,
        visible_only = opts.visible_only,
        center = opts.center,
      })
    end)
  end
  vim.bo[bufnr].modifiable = false
  active_effects[bufnr] = effect

  vim.defer_fn(function()
    if not is_current_generation() then
      return
    end

    local current = active_effects[bufnr]
    if not current or current.id ~= id then
      return
    end

    play_glitch_sound(effect.frames * effect.frame_delay_ms)
    render_frame(bufnr, effect, 1)
  end, math.max(config.start_delay_ms, 0))

  return true
end

local function queue_reload(bufnr, path)
  if not is_current_generation() or pending_reload[path] then
    return
  end

  local suppress_until = recent_manual_writes[path]
  if suppress_until and suppress_until > uv.now() then
    pending_reload[path] = true
    local delay_ms = math.max(suppress_until - uv.now(), 1)
    vim.defer_fn(function()
      if pending_reload[path] ~= true then
        return
      end

      pending_reload[path] = nil
      queue_reload(bufnr, path)
    end, delay_ms)
    return
  end
  recent_manual_writes[path] = nil

  pending_reload[path] = true

  vim.defer_fn(function()
    pending_reload[path] = nil

    if not is_current_generation()
      or not vim.api.nvim_buf_is_valid(bufnr)
      or not is_buf_visible_in_current_tab(bufnr)
    then
      return
    end

    local current_path = normalize_path(vim.api.nvim_buf_get_name(bufnr))
    if current_path ~= path or vim.fn.filereadable(path) ~= 1 then
      return
    end

    if not is_fx_enabled() then
      vim.cmd(string.format("silent! checktime %d", bufnr))
      return
    end

    local old_lines = buf_lines(bufnr)
    local ok, disk_lines = pcall(vim.fn.readfile, path)
    if not ok or type(disk_lines) ~= "table" then
      snapshots[bufnr] = old_lines
      vim.cmd(string.format("silent! checktime %d", bufnr))
      return
    end

    if vim.deep_equal(old_lines, disk_lines) then
      return
    end

    local hunks = vim.diff(join_lines(old_lines), join_lines(disk_lines), {
      result_type = "indices",
      algorithm = "histogram",
    }) or {}

    if #hunks == 0 then
      snapshots[bufnr] = old_lines
      vim.cmd(string.format("silent! checktime %d", bufnr))
      return
    end

    if not start_pre_delete_effect(bufnr, old_lines, disk_lines, path, hunks) then
      if reload_buffer_preserving_view(bufnr) then
        vim.schedule(function()
          if not is_current_generation()
            or not vim.api.nvim_buf_is_valid(bufnr)
            or not is_buf_visible_in_current_tab(bufnr)
          then
            return
          end

          start_effect(bufnr, old_lines, {
            start_delay_ms = 0,
            include_delete_states = false,
          })
        end)
      else
        snapshots[bufnr] = old_lines
        pending_post_effect_opts[bufnr] = {
          start_delay_ms = 0,
          include_delete_states = false,
        }
        vim.cmd(string.format("silent! checktime %d", bufnr))
      end
    end
  end, math.max(config.watch_debounce_ms, 1))
end

local follow_tool_name = "report_file_edits"

local function notify_follow_registration_error(err)
  vim.schedule(function()
    vim.notify("Failed to register Naia `" .. follow_tool_name .. "` tool: " .. tostring(err), vim.log.levels.WARN)
  end)
end

local function resolve_reported_follow_path(path)
  if type(path) ~= "string" then
    path = tostring(path or "")
  end

  path = vim.trim(path)
  if path == "" then
    return nil
  end

  return normalize_path(vim.fs.abspath(path))
end

local function naia_report_file_edits(args)
  local path = resolve_reported_follow_path(args and args.path or args and args.file_path or args and args.filename)
  local hunks = normalize_reported_hunks(args and args.hunks or nil)

  if not path then
    error("report_file_edits requires a non-empty path")
  end

  if not hunks then
    error("report_file_edits requires at least one valid hunk")
  end

  if not is_ai_report_trigger_enabled() then
    push_follow_debug("ignored reported file edits while AI report trigger is disabled: " .. path)
    return ""
  end

  local visible_bufnr = vim.fn.bufnr(path)
  if is_external_change_trigger_enabled()
    and visible_bufnr > 0
    and is_buf_visible_in_any_window(visible_bufnr)
  then
    push_follow_debug("deferring AI-reported edit to external-change flow for visible buffer: " .. path)
    return ""
  end

  local ok, started, err = pcall(function()
    return play_reported_follow_change(path, hunks)
  end)

  if not ok then
    error(started)
  end

  if not started then
    return ""
  end

  return ""
end

local function register_follow_naia_tool()
  local ok, naia = pcall(require, "naia")
  if not ok then
    return
  end

  pcall(naia.deregister_tool, follow_tool_name)

  local registered, err = naia.register_tool(follow_tool_name, {
    title = "Report File Edits",
    description = "Report AI-driven file edits so Neovim can play file change FX for the given file and changed line ranges.",
    input_schema = {
      type = "object",
      properties = {
        path = {
          type = "string",
          description = "The edited file path, absolute or relative to the current Neovim working directory.",
        },
        hunks = {
          type = "array",
          description = "Changed line ranges in the edited file.",
          minItems = 1,
          items = {
            type = "object",
            properties = {
              kind = {
                type = "string",
                description = "One of add, mod, del, addition, modification, or deletion.",
              },
              start_line = {
                type = "integer",
                description = "The 1-based start line for the changed hunk.",
              },
              end_line = {
                type = "integer",
                description = "The 1-based end line for the changed hunk.",
              },
            },
            required = { "kind", "start_line", "end_line" },
            additionalProperties = false,
          },
        },
      },
      required = { "path", "hunks" },
      additionalProperties = false,
    },
    callback = naia_report_file_edits,
  })

  if not registered then
    notify_follow_registration_error(err)
  end
end

local function stop_watcher(path)
  local state = watchers[path]
  if not state then
    return
  end

  watchers[path] = nil
  pending_reload[path] = nil

  pcall(function()
    if state.handle and not state.handle:is_closing() then
      state.handle:stop()
      state.handle:close()
    end
  end)
end

local function ensure_watcher(bufnr, path)
  if watchers[path] then
    watchers[path].bufnr = bufnr
    return
  end

  local dir = vim.fs.dirname(path)
  local basename = vim.fs.basename(path)
  if not dir or dir == "" or not basename or basename == "" then
    return
  end

  local handle = uv.new_fs_event()
  if not handle then
    return
  end

  local ok, err = pcall(handle.start, handle, dir, {}, vim.schedule_wrap(function(watch_err, filename)
    if watch_err or not is_current_generation() then
      return
    end

    local state = watchers[path]
    if not state then
      return
    end

    if filename and filename ~= "" then
      local changed_path = normalize_path(vim.fs.joinpath(dir, filename))
      if changed_path ~= path then
        return
      end
    end

    queue_reload(state.bufnr, path)
  end))

  if not ok then
    pcall(function()
      if not handle:is_closing() then
        handle:close()
      end
    end)
    vim.schedule(function()
      vim.notify("file-change-fx watcher failed: " .. tostring(err), vim.log.levels.WARN)
    end)
    return
  end

  watchers[path] = {
    handle = handle,
    bufnr = bufnr,
    dir = dir,
    basename = basename,
  }
end

local function refresh_watchers()
  if not is_current_generation() then
    return
  end

  if not is_external_change_trigger_enabled() then
    for path in pairs(watchers) do
      stop_watcher(path)
    end
    return
  end

  local needed = {}
  for _, item in ipairs(current_tab_visible_file_bufs()) do
    needed[item.path] = item.bufnr
    ensure_watcher(item.bufnr, item.path)
  end

  for path in pairs(watchers) do
    if not needed[path] then
      stop_watcher(path)
    end
  end
end

vim.api.nvim_create_autocmd({
  "BufEnter",
  "BufWinEnter",
  "BufWinLeave",
  "DirChanged",
  "TabEnter",
  "TabClosed",
  "WinEnter",
  "WinClosed",
}, {
  group = group,
  callback = function(args)
    if args.event == "DirChanged" then
      push_follow_debug("working directory changed to " .. tostring(normalize_path(vim.fn.getcwd())))
    end
    refresh_watchers()
  end,
})

vim.api.nvim_create_autocmd("FileChangedShell", {
  group = group,
  callback = function(args)
    if not vim.api.nvim_buf_is_valid(args.buf) then
      return
    end

    if guarded_pre_reload[args.buf] and not intentional_reload[args.buf] then
      vim.v.fcs_choice = ""
      return
    end

    snapshots[args.buf] = snapshots[args.buf] or buf_lines(args.buf)
    vim.v.fcs_choice = "reload"
  end,
})

vim.api.nvim_create_autocmd("FileChangedShellPost", {
  group = group,
  callback = function(args)
    local old_lines = snapshots[args.buf]
    local opts = pending_post_effect_opts[args.buf]
    snapshots[args.buf] = nil
    pending_post_effect_opts[args.buf] = nil
    intentional_reload[args.buf] = nil
    restore_autoread(args.buf)
    if not old_lines then
      return
    end

    if not is_fx_enabled() or not is_external_change_trigger_enabled() then
      vim.schedule(refresh_watchers)
      return
    end

    start_effect(args.buf, old_lines, opts)
    vim.schedule(refresh_watchers)
  end,
})

vim.api.nvim_create_autocmd("BufDelete", {
  group = group,
  callback = function(args)
    snapshots[args.buf] = nil
    pending_post_effect_opts[args.buf] = nil
    pending_save_snapshots[args.buf] = nil
    intentional_reload[args.buf] = nil
    restore_autoread(args.buf)
    active_effects[args.buf] = nil
    vim.schedule(refresh_watchers)
  end,
})

vim.api.nvim_create_autocmd("VimLeavePre", {
  group = group,
  callback = function() end,
})

vim.api.nvim_create_autocmd("BufWritePre", {
  group = group,
  callback = function(args)
    if not is_fx_enabled()
      or not vim.api.nvim_buf_is_valid(args.buf)
      or vim.bo[args.buf].buftype ~= ""
    then
      return
    end

    local path = normalize_path(vim.api.nvim_buf_get_name(args.buf))
    if not path or vim.fn.filereadable(path) ~= 1 then
      pending_save_snapshots[args.buf] = nil
      return
    end

    local ok, disk_lines = pcall(vim.fn.readfile, path)
    if not ok or type(disk_lines) ~= "table" then
      pending_save_snapshots[args.buf] = nil
      return
    end

    pending_save_snapshots[args.buf] = disk_lines
  end,
})

vim.api.nvim_create_autocmd("BufWritePost", {
  group = group,
  callback = function(args)
    local old_lines = pending_save_snapshots[args.buf]
    pending_save_snapshots[args.buf] = nil
    local path = normalize_path(vim.api.nvim_buf_get_name(args.buf))
    if path then
      recent_manual_writes[path] = uv.now() + math.max(config.watch_debounce_ms * 4, 300)
    end

    if not old_lines
      or not is_fx_enabled()
      or not is_save_trigger_enabled()
      or not vim.api.nvim_buf_is_valid(args.buf)
      or not is_buf_visible_in_current_tab(args.buf)
      or vim.bo[args.buf].buftype ~= ""
    then
      return
    end

    start_effect(args.buf, old_lines, {
      start_delay_ms = 0,
      include_delete_states = false,
      visible_only = true,
      center = false,
    })
  end,
})

vim.api.nvim_create_user_command("FileChangeFxCheck", function()
  vim.cmd("checktime")
end, {})

vim.api.nvim_create_user_command("FileChangeFxToggle", function()
  persisted.enabled = not is_fx_enabled()

  if not persisted.enabled then
    disable_all_effects()
  end

  local status = persisted.enabled and "enabled" or "disabled"
  vim.notify("file-change-fx " .. status, vim.log.levels.INFO, { title = "FileChangeFxToggle" })
end, {})

vim.api.nvim_create_user_command("FileChangeFxToggleOnSave", function()
  persisted.trigger_on_save = not is_save_trigger_enabled()

  local status = persisted.trigger_on_save and "enabled" or "disabled"
  vim.notify("file-change-fx on-save trigger " .. status, vim.log.levels.INFO, {
    title = "FileChangeFxToggleOnSave",
  })
end, {})

vim.api.nvim_create_user_command("FileChangeFxToggleOnExternalChange", function()
  persisted.trigger_on_external_change = not is_external_change_trigger_enabled()
  vim.schedule(refresh_watchers)

  local status = persisted.trigger_on_external_change and "enabled" or "disabled"
  vim.notify("file-change-fx external-change trigger " .. status, vim.log.levels.INFO, {
    title = "FileChangeFxToggleOnExternalChange",
  })
end, {})

vim.api.nvim_create_user_command("FileChangeFxToggleOnAiReport", function()
  persisted.trigger_on_ai_report = not is_ai_report_trigger_enabled()

  local status = persisted.trigger_on_ai_report and "enabled" or "disabled"
  vim.notify("file-change-fx AI-report trigger " .. status, vim.log.levels.INFO, {
    title = "FileChangeFxToggleOnAiReport",
  })
end, {})

vim.api.nvim_create_user_command("FileChangeFxWatchInfo", function()
  refresh_watchers()

  local lines = {
    string.format("file-change-fx generation=%s", tostring(generation)),
    string.format("watchers=%d", vim.tbl_count(watchers)),
  }

  local paths = vim.tbl_keys(watchers)
  table.sort(paths)

  if #paths == 0 then
    lines[#lines + 1] = "(no visible file buffers are being watched)"
  else
    for _, path in ipairs(paths) do
      local state = watchers[path]
      local bufnr = state and state.bufnr or -1
      local visible = bufnr >= 0 and is_buf_visible_in_current_tab(bufnr) or false
      lines[#lines + 1] = string.format("- buf=%d visible=%s %s", bufnr, visible and "yes" or "no", path)
    end
  end

  vim.notify(table.concat(lines, "\n"), vim.log.levels.INFO, { title = "FileChangeFxWatchInfo" })
end, {})

vim.api.nvim_create_user_command("FileChangeFxTestSound", function(opts)
  local seconds = tonumber(opts.args)
  if not seconds or seconds <= 0 then
    vim.notify("FileChangeFxTestSound requires a positive duration in seconds", vim.log.levels.ERROR)
    return
  end

  local duration_ms = math.max(math.floor(seconds * 1000), 1)
  play_glitch_sound(duration_ms)
end, {
  nargs = 1,
  desc = "Play the file-change-fx glitch sound for the given duration in seconds",
})

-- register_follow_naia_tool()
-- vim.schedule(register_follow_naia_tool)
vim.schedule(refresh_watchers)
