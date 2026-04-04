local config = vim.tbl_deep_extend("force", {
  start_delay_ms = 350,
  radius = 100,
  frames = 20,
  frame_delay_ms = 33,
  watch_debounce_ms = 80,
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
local watchman_watch_root = nil
local watchman_relative_root = nil
local watchman_clock = nil
local follow_poll_generation = 0
local watchman_debug_log = {}
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
persisted.follow_enabled = false

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

local function is_follow_enabled()
  return _G.__file_change_fx_state and _G.__file_change_fx_state.follow_enabled == true
end

local function watchman_debug_enabled()
  return _G.__file_change_fx_state and _G.__file_change_fx_state.follow_debug == true
end

local function normalize_path(path)
  if not path or path == "" then
    return nil
  end

  return vim.fs.normalize(path)
end

local function watchman_base_dir()
  if watchman_relative_root and watchman_relative_root ~= "" then
    return normalize_path(vim.fs.joinpath(watchman_watch_root, watchman_relative_root))
  end

  return normalize_path(watchman_watch_root)
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

local function push_watchman_debug(message, level)
  local entry = string.format("[%s] %s", os.date("%H:%M:%S"), tostring(message))
  watchman_debug_log[#watchman_debug_log + 1] = entry
  if #watchman_debug_log > 120 then
    table.remove(watchman_debug_log, 1)
  end

  if watchman_debug_enabled() then
    vim.schedule(function()
      vim.notify(entry, level or vim.log.levels.INFO, { title = "FileChangeFollowDebug" })
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

local function is_normal_file_edit_win(win)
  if not vim.api.nvim_win_is_valid(win) or vim.fn.win_gettype(win) ~= "" then
    return false
  end

  local bufnr = vim.api.nvim_win_get_buf(win)
  return vim.api.nvim_buf_is_valid(bufnr) and vim.bo[bufnr].buftype == ""
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

local function open_buffer_in_best_window(bufnr)
  local existing = get_target_win(bufnr)
  if existing and vim.api.nvim_win_is_valid(existing) then
    return existing
  end

  local win = best_editing_win()
  if not win then
    return nil
  end

  local ok = pcall(vim.api.nvim_win_set_buf, win, bufnr)
  if not ok then
    return nil
  end

  return win
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

local function render_frame(bufnr, effect, frame)
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

local function load_buffer_for_follow(path)
  local bufnr = vim.fn.bufnr(path)
  local old_lines = {}

  if bufnr > 0 and vim.api.nvim_buf_is_valid(bufnr) then
    if vim.bo[bufnr].modified then
      return nil, nil
    end
    old_lines = buf_lines(bufnr)
  else
    bufnr = vim.fn.bufadd(path)
    if bufnr <= 0 then
      return nil, nil
    end
    vim.fn.bufload(bufnr)
  end

  local win = open_buffer_in_best_window(bufnr)
  if not win then
    return nil, nil
  end

  return bufnr, old_lines
end

local function handle_follow_change(path)
  if vim.fn.filereadable(path) ~= 1 then
    push_watchman_debug("ignoring unreadable path: " .. tostring(path), vim.log.levels.WARN)
    return
  end

  push_watchman_debug("handling follow change for " .. path)
  local bufnr, old_lines = load_buffer_for_follow(path)
  if not bufnr then
    push_watchman_debug("could not load/open buffer for " .. path, vim.log.levels.WARN)
    return
  end

  push_watchman_debug(string.format("loaded bufnr=%d for %s", bufnr, path))
  if reload_buffer_preserving_view(bufnr) then
    push_watchman_debug(string.format("reloaded bufnr=%d for %s", bufnr, path))
    vim.schedule(function()
      if not is_current_generation() or not vim.api.nvim_buf_is_valid(bufnr) then
        return
      end

      start_effect(bufnr, old_lines, {
        start_delay_ms = 0,
        include_delete_states = false,
      })
    end)
  else
    push_watchman_debug(string.format("reload failed; falling back to checktime for bufnr=%d path=%s", bufnr, path), vim.log.levels.WARN)
    snapshots[bufnr] = old_lines
    pending_post_effect_opts[bufnr] = {
      start_delay_ms = 0,
      include_delete_states = false,
    }
    vim.cmd(string.format("silent! checktime %d", bufnr))
  end
end

local function stop_watchman_follow()
  push_watchman_debug("stopping watchman follow")
  follow_poll_generation = follow_poll_generation + 1
  watchman_watch_root = nil
  watchman_relative_root = nil
  watchman_clock = nil
end

local function refresh_follow_scan()
  if not is_current_generation() then
    return
  end

  if not is_follow_enabled() then
    stop_watchman_follow()
    return
  end

  push_watchman_debug("refresh_follow_scan cwd=" .. tostring(normalize_path(vim.fn.getcwd())))

  if vim.fn.executable("watchman") ~= 1 then
    persisted.follow_enabled = false
    stop_watchman_follow()
    vim.schedule(function()
      vim.notify("FileChangeFollow requires watchman to be installed", vim.log.levels.ERROR, {
        title = "FileChangeFollow",
      })
    end)
    return
  end

  local root = normalize_path(vim.fn.getcwd())
  if not root or vim.fn.isdirectory(root) ~= 1 then
    return
  end

  local current_base = watchman_base_dir()
  if current_base and current_base ~= root then
    stop_watchman_follow()
  end

  local watch_project_output = vim.fn.system({
    "watchman",
    "--output-encoding=json",
    "--no-pretty",
    "watch-project",
    root,
  })

  if vim.v.shell_error ~= 0 then
    persisted.follow_enabled = false
    stop_watchman_follow()
    vim.schedule(function()
      vim.notify(
        "file-change-fx watchman watch-project failed: " .. tostring(watch_project_output),
        vim.log.levels.ERROR,
        { title = "FileChangeFollow" }
      )
    end)
    return
  end

  push_watchman_debug("watch-project raw response: " .. tostring(watch_project_output))

  local ok, watch_project = pcall(vim.json.decode, watch_project_output)
  if not ok or type(watch_project) ~= "table" or not watch_project.watch then
    persisted.follow_enabled = false
    stop_watchman_follow()
    vim.schedule(function()
      vim.notify("file-change-fx got an invalid watchman watch-project response", vim.log.levels.ERROR, {
        title = "FileChangeFollow",
      })
    end)
    return
  end

  watchman_watch_root = normalize_path(watch_project.watch)
  watchman_relative_root = watch_project.relative_path
  push_watchman_debug(
    "watch-project resolved root="
      .. tostring(watchman_watch_root)
      .. " relative_root="
      .. tostring(watchman_relative_root)
  )
  if watchman_clock then
    return
  end

  local clock_output = vim.fn.system({
    "watchman",
    "--output-encoding=json",
    "--no-pretty",
    "clock",
    watchman_watch_root,
  })

  if vim.v.shell_error ~= 0 then
    persisted.follow_enabled = false
    stop_watchman_follow()
    vim.schedule(function()
      vim.notify("file-change-fx watchman clock failed: " .. tostring(clock_output), vim.log.levels.ERROR, {
        title = "FileChangeFollow",
      })
    end)
    return
  end

  push_watchman_debug("watchman clock raw response: " .. tostring(clock_output))

  local clock_ok, clock_response = pcall(vim.json.decode, clock_output)
  if not clock_ok or type(clock_response) ~= "table" or not clock_response.clock then
    persisted.follow_enabled = false
    stop_watchman_follow()
    vim.schedule(function()
      vim.notify("file-change-fx got an invalid watchman clock response", vim.log.levels.ERROR, {
        title = "FileChangeFollow",
      })
    end)
    return
  end

  watchman_clock = clock_response.clock
  push_watchman_debug("watchman clock initialized to " .. tostring(watchman_clock))

  local poll_generation = follow_poll_generation + 1
  follow_poll_generation = poll_generation

  local function poll_watchman_changes()
    if not is_current_generation() or not is_follow_enabled() or poll_generation ~= follow_poll_generation then
      return
    end

    if not watchman_watch_root or not watchman_clock then
      return
    end

    local query = {
      since = watchman_clock,
      expression = { "allof", { "type", "f" } },
      fields = { "name", "exists", "type" },
    }
    if watchman_relative_root and watchman_relative_root ~= "" then
      query.relative_root = watchman_relative_root
    end

    local query_output = vim.fn.system({
      "watchman",
      "--server-encoding=json",
      "--output-encoding=json",
      "--no-pretty",
      "-j",
    }, vim.json.encode({ "query", watchman_watch_root, query }))

    if vim.v.shell_error ~= 0 then
      push_watchman_debug("watchman query failed: " .. tostring(query_output), vim.log.levels.WARN)
    else
      push_watchman_debug("watchman query raw response: " .. tostring(query_output))
      local query_ok, query_response = pcall(vim.json.decode, query_output)
      if query_ok and type(query_response) == "table" then
        if query_response.error then
          push_watchman_debug("watchman query error: " .. tostring(query_response.error), vim.log.levels.WARN)
        else
          if query_response.clock then
            watchman_clock = query_response.clock
          end

          local base_dir = watchman_base_dir()
          if type(query_response.files) == "table" and base_dir then
            for _, file in ipairs(query_response.files) do
              if type(file) == "table" and file.exists ~= false and (file.type == nil or file.type == "f") then
                local name = file.name
                if type(name) == "string" and name ~= "" then
                  local path = normalize_path(vim.fs.joinpath(base_dir, name))
                  push_watchman_debug("watchman event file=" .. name .. " resolved=" .. tostring(path))
                  local suppress_until = recent_manual_writes[path]
                  if not (suppress_until and suppress_until > uv.now()) then
                    handle_follow_change(path)
                  else
                    push_watchman_debug("suppressed recent manual write for " .. path)
                  end
                end
              end
            end
          end
        end
      else
        push_watchman_debug("failed to decode watchman query response", vim.log.levels.WARN)
      end
    end

    vim.defer_fn(poll_watchman_changes, math.max(config.watch_debounce_ms * 4, 250))
  end

  vim.defer_fn(poll_watchman_changes, math.max(config.watch_debounce_ms * 4, 250))
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

  refresh_follow_scan()
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
      stop_watchman_follow()
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

    if not is_fx_enabled() then
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
  callback = function()
    stop_watchman_follow()
  end,
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
    stop_watchman_follow()
  end

  local status = persisted.enabled and "enabled" or "disabled"
  vim.notify("file-change-fx " .. status, vim.log.levels.INFO, { title = "FileChangeFxToggle" })
end, {})

vim.api.nvim_create_user_command("FileChangeFollow", function()
  local enable = not is_follow_enabled()
  if enable and vim.fn.executable("watchman") ~= 1 then
    vim.notify("FileChangeFollow requires watchman to be installed", vim.log.levels.ERROR, {
      title = "FileChangeFollow",
    })
    return
  end

  persisted.follow_enabled = enable
  refresh_follow_scan()

  local status = persisted.follow_enabled and "enabled" or "disabled"
  vim.notify("file-change-fx follow " .. status, vim.log.levels.INFO, { title = "FileChangeFollow" })
end, {})

vim.api.nvim_create_user_command("FileChangeFollowInfo", function()
  local running = persisted.follow_enabled == true and watchman_clock ~= nil
  local lines = {
    string.format("follow_enabled=%s", persisted.follow_enabled == true and "yes" or "no"),
    string.format("watchman_running=%s", running and "yes" or "no"),
    string.format("cwd=%s", normalize_path(vim.fn.getcwd()) or "(none)"),
    string.format("watch_root=%s", watchman_watch_root or "(none)"),
    string.format("relative_root=%s", watchman_relative_root or "(none)"),
    string.format("clock=%s", watchman_clock or "(none)"),
  }

  vim.notify(table.concat(lines, "\n"), vim.log.levels.INFO, { title = "FileChangeFollowInfo" })
end, {})

vim.api.nvim_create_user_command("FileChangeFollowDebug", function()
  persisted.follow_debug = not watchman_debug_enabled()
  local status = persisted.follow_debug and "enabled" or "disabled"
  vim.notify("file-change-fx follow debug " .. status, vim.log.levels.INFO, {
    title = "FileChangeFollowDebug",
  })
end, {})

vim.api.nvim_create_user_command("FileChangeFollowDebugLog", function()
  local lines = vim.deepcopy(watchman_debug_log)
  if #lines == 0 then
    lines = { "(no follow debug entries yet)" }
  end
  vim.notify(table.concat(lines, "\n"), vim.log.levels.INFO, {
    title = "FileChangeFollowDebugLog",
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

vim.schedule(refresh_watchers)
