local config = vim.tbl_deep_extend("force", {
  start_delay_ms = 350,
  radius = 100,
  frames = 30,
  frame_delay_ms = 35,
  watch_debounce_ms = 80,
  charset = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789!@#$%^&*()-_=+[]{};:,.<>/?\\|",
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
local guarded_pre_reload = {}
local intentional_reload = {}

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

vim.opt.autoread = true

local function set_transparent_fx_highlights()
  local function make_hl(name, spec)
    spec.bg = "NONE"
    spec.ctermbg = "NONE"
    vim.api.nvim_set_hl(0, name, spec)
  end

  make_hl("FileChangeFxAdd", { fg = "#66ff99", bold = true })
  make_hl("FileChangeFxAddNoise", { fg = "#2ee66b", bold = true, nocombine = true })

  make_hl("FileChangeFxMod", { fg = "#66b3ff", bold = true })
  make_hl("FileChangeFxModNoise", { fg = "#2f7fff", bold = true, nocombine = true })

  make_hl("FileChangeFxDel", { fg = "#ff6b6b", bold = true })
  make_hl("FileChangeFxDelNoise", { fg = "#ff3b3b", bold = true, nocombine = true })
end

set_transparent_fx_highlights()

local function is_current_generation()
  return _G.__file_change_fx_state and _G.__file_change_fx_state.generation == generation
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
  local start_line = math.max(1, center_line - config.radius)
  local end_line = math.min(#new_lines, center_line + config.radius)
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

local function build_pre_delete_state(old_lines, new_lines, hunks, center_line)
  local start_line = math.max(1, center_line - config.radius)
  local end_line = math.min(#old_lines, center_line + config.radius)
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
end

local function restore_autoread(bufnr)
  local guard = guarded_pre_reload[bufnr]
  if not guard then
    return
  end

  guarded_pre_reload[bufnr] = nil

  if vim.api.nvim_buf_is_valid(bufnr) then
    vim.bo[bufnr].autoread = guard.autoread
  end
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
    clear_effect(bufnr)
    if type(on_complete) == "function" then
      on_complete()
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
  local effect = build_window_state(old_lines, new_lines, hunks, center_line, {
    include_delete_states = opts.include_delete_states,
  })
  if not next(effect.line_states) then
    return
  end

  center_on_line(bufnr, center_line)

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

    render_frame(bufnr, effect, 1)
  end, math.max(opts.start_delay_ms ~= nil and opts.start_delay_ms or config.start_delay_ms, 0))
end

local function start_pre_delete_effect(bufnr, old_lines, new_lines, path, hunks)
  if not is_current_generation() or not vim.api.nvim_buf_is_valid(bufnr) or not is_buf_visible_in_current_tab(bufnr) then
    return false
  end

  local center_line = choose_old_hunk_center(hunks)
  local effect = build_pre_delete_state(old_lines, new_lines, hunks, center_line)
  if not next(effect.line_states) then
    return false
  end

  center_on_line(bufnr, center_line)

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

    local win = get_target_win(bufnr)
    local view = nil
    if win and vim.api.nvim_win_is_valid(win) then
      view = vim.api.nvim_win_call(win, function()
        return vim.fn.winsaveview()
      end)
    end

    restore_autoread(bufnr)

    local reloaded = false
    if win and vim.api.nvim_win_is_valid(win) then
      reloaded = pcall(vim.api.nvim_win_call, win, function()
        vim.cmd("silent! edit!")
      end)
    end

    if not reloaded then
      snapshots[bufnr] = old_lines
      pending_post_effect_opts[bufnr] = {
        start_delay_ms = 0,
        include_delete_states = false,
      }
      intentional_reload[bufnr] = true
      vim.cmd(string.format("silent! checktime %d", bufnr))
      return
    end

    if view and win and vim.api.nvim_win_is_valid(win) then
      pcall(vim.api.nvim_win_call, win, function()
        vim.fn.winrestview(view)
      end)
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

    render_frame(bufnr, effect, 1)
  end, math.max(config.start_delay_ms, 0))

  return true
end

local function queue_reload(bufnr, path)
  if not is_current_generation() or pending_reload[path] then
    return
  end

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
      snapshots[bufnr] = old_lines
      pending_post_effect_opts[bufnr] = {
        start_delay_ms = 0,
        include_delete_states = false,
      }
      vim.cmd(string.format("silent! checktime %d", bufnr))
    end
  end, math.max(config.watch_debounce_ms, 1))
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

  local handle = uv.new_fs_event()
  if not handle then
    return
  end

  local ok, err = pcall(handle.start, handle, path, {}, vim.schedule_wrap(function(watch_err)
    if watch_err or not is_current_generation() then
      return
    end

    local state = watchers[path]
    if not state then
      return
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
end

vim.api.nvim_create_autocmd({
  "BufEnter",
  "BufWinEnter",
  "BufWinLeave",
  "TabEnter",
  "TabClosed",
  "WinEnter",
  "WinClosed",
}, {
  group = group,
  callback = refresh_watchers,
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

    start_effect(args.buf, old_lines, opts)
    vim.schedule(refresh_watchers)
  end,
})

vim.api.nvim_create_autocmd("BufDelete", {
  group = group,
  callback = function(args)
    snapshots[args.buf] = nil
    pending_post_effect_opts[args.buf] = nil
    intentional_reload[args.buf] = nil
    restore_autoread(args.buf)
    active_effects[args.buf] = nil
    vim.schedule(refresh_watchers)
  end,
})

vim.api.nvim_create_user_command("FileChangeFxCheck", function()
  vim.cmd("checktime")
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

vim.schedule(refresh_watchers)
