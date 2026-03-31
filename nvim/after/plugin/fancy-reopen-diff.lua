local add_namespace = vim.api.nvim_create_namespace("fancy_reopen_diff_add")
local delete_namespace = vim.api.nvim_create_namespace("fancy_reopen_diff_delete")
local existing_runtime = rawget(vim, "_fancy_reopen_diff_runtime")
local state = {}
local uv = vim.uv or vim.loop
local play_animation
local glitch_char
local max_animated_file_lines = 5000
local max_animated_file_bytes = 512 * 1024
local max_animated_changed_lines = 200
local animation_start_delay_ms = 120
local animation_step_ms = 16
local git_hunks_frame_count = 10
local digital_wipe_reveal_frames = 20
local digital_wipe_fade_frames = 20
local reload_settle_delay_ms = 30
local reload_retry_count = 6
local watcher_checktime_debounce_ms = 120

vim.o.autoread = true
math.randomseed(tonumber(tostring(uv.hrtime()):sub(-9)))

local function set_highlights()
  vim.api.nvim_set_hl(0, "FancyReopenDiffAddCore", { bg = "#2f8f63", bold = true })
  vim.api.nvim_set_hl(0, "FancyReopenDiffAddTrail", { bg = "#194532" })
  vim.api.nvim_set_hl(0, "FancyReopenDiffChangeCore", { bg = "#b58900", bold = true })
  vim.api.nvim_set_hl(0, "FancyReopenDiffChangeTrail", { bg = "#5c4b16" })
  vim.api.nvim_set_hl(0, "FancyReopenDiffDelete", { bg = "#4a1f24", fg = "#ffb8c0" })
  vim.api.nvim_set_hl(0, "FancyReopenDiffDeleteInline", { bg = "#4a1f24", fg = "#ffb8c0" })
  vim.api.nvim_set_hl(0, "FancyReopenDiffDeletePrefix", { fg = "#ff6b7d", bold = true })
  vim.api.nvim_set_hl(0, "FancyReopenDiffAddSign", { fg = "#63d297" })
  vim.api.nvim_set_hl(0, "FancyReopenDiffChangeSign", { fg = "#ffd75f" })
  vim.api.nvim_set_hl(0, "FancyReopenDiffWipeDense", { fg = "#8cf2ff", bg = "#0b1f2a", bold = true })
  vim.api.nvim_set_hl(0, "FancyReopenDiffWipeMid", { fg = "#5dd9f5", bg = "#08161f" })
  vim.api.nvim_set_hl(0, "FancyReopenDiffWipeLight", { fg = "#3bb6d6", bg = "#050d12" })
  vim.api.nvim_set_hl(0, "FancyReopenDiffWipeGhost", { fg = "#1d5363", bg = "#03070a" })
  vim.api.nvim_set_hl(0, "FancyReopenDiffWipeHot", { fg = "#b7ff7a", bg = "#10210c", bold = true })
  vim.api.nvim_set_hl(0, "FancyReopenDiffWipeTrace", { fg = "#6dffb3", bg = "#08140f" })
  vim.api.nvim_set_hl(0, "FancyReopenDiffMorphDeleteHot", { fg = "#ff8ba0", bold = true })
  vim.api.nvim_set_hl(0, "FancyReopenDiffMorphDeleteMid", { fg = "#ff6b7d" })
  vim.api.nvim_set_hl(0, "FancyReopenDiffMorphDeleteGhost", { fg = "#7a3743", italic = true })
end

set_highlights()

local visual_modes = {}
local default_visual_mode = "digital_wipe"

local function joined(lines)
  return table.concat(lines, "\n")
end

local function same_lines(a, b)
  if #a ~= #b then
    return false
  end

  for i = 1, #a do
    if a[i] ~= b[i] then
      return false
    end
  end

  return true
end

local function is_normal_file_buffer(bufnr)
  return vim.api.nvim_buf_is_valid(bufnr)
    and vim.bo[bufnr].buftype == ""
    and vim.api.nvim_buf_get_name(bufnr) ~= ""
end

local function is_buffer_visible(bufnr)
  return is_normal_file_buffer(bufnr) and #vim.fn.win_findbuf(bufnr) > 0
end

local function file_stat(path)
  return path ~= "" and uv.fs_stat(path) or nil
end

local function same_file_stat(a, b)
  if not a or not b then
    return false
  end

  local a_mtime = a.mtime or {}
  local b_mtime = b.mtime or {}

  return a.size == b.size
    and a.type == b.type
    and a_mtime.sec == b_mtime.sec
    and a_mtime.nsec == b_mtime.nsec
end

local function clear_animation(bufnr)
  if vim.api.nvim_buf_is_valid(bufnr) then
    vim.api.nvim_buf_clear_namespace(bufnr, add_namespace, 0, -1)
    vim.api.nvim_buf_clear_namespace(bufnr, delete_namespace, 0, -1)
  end
end

local function stop_animation_timer(bufnr)
  local buffer_state = state[bufnr]
  if not (buffer_state and buffer_state.timer) then
    return
  end

  local timer = buffer_state.timer
  buffer_state.timer = nil

  pcall(timer.stop, timer)
  pcall(timer.close, timer)
end

local function stop_file_watcher(bufnr)
  local buffer_state = state[bufnr]
  if not (buffer_state and buffer_state.watcher) then
    return
  end

  local watcher = buffer_state.watcher
  buffer_state.watcher = nil
  buffer_state.watcher_path = nil

  pcall(watcher.stop, watcher)
  pcall(watcher.close, watcher)
end

local function restore_buffer_modifiable(bufnr)
  local buffer_state = state[bufnr]
  if buffer_state and buffer_state.restore_modifiable ~= nil and vim.api.nvim_buf_is_valid(bufnr) then
    vim.bo[bufnr].modifiable = buffer_state.restore_modifiable
    buffer_state.restore_modifiable = nil
  end
end

if existing_runtime and type(existing_runtime.cleanup_all) == "function" then
  existing_runtime.cleanup_all()
end

local function schedule_checktime(bufnr, delay_ms)
  if not is_normal_file_buffer(bufnr) or vim.bo[bufnr].modified then
    return
  end

  state[bufnr] = state[bufnr] or {}
  state[bufnr].checktime_request_id = (state[bufnr].checktime_request_id or 0) + 1

  local request_id = state[bufnr].checktime_request_id
  vim.defer_fn(function()
    local buffer_state = state[bufnr]
    if not buffer_state or buffer_state.checktime_request_id ~= request_id then
      return
    end

    if not is_normal_file_buffer(bufnr) or vim.bo[bufnr].modified then
      return
    end

    vim.cmd(string.format("silent! checktime %d", bufnr))
  end, delay_ms or 0)
end

local function ensure_file_watcher(bufnr)
  if not is_buffer_visible(bufnr) then
    stop_file_watcher(bufnr)
    return
  end

  local path = vim.api.nvim_buf_get_name(bufnr)
  local buffer_state = state[bufnr] or {}
  state[bufnr] = buffer_state

  if path == "" then
    stop_file_watcher(bufnr)
    return
  end

  if buffer_state.watcher and buffer_state.watcher_path == path then
    return
  end

  stop_file_watcher(bufnr)

  local watcher = uv.new_fs_event()
  if not watcher then
    return
  end

  local ok = watcher:start(path, {}, function(err)
    if err then
      return
    end

    vim.schedule(function()
      if not is_buffer_visible(bufnr) then
        stop_file_watcher(bufnr)
        return
      end

      local current_path = vim.api.nvim_buf_get_name(bufnr)
      if current_path == "" then
        return
      end

      schedule_checktime(bufnr, watcher_checktime_debounce_ms)
    end)
  end)

  if not ok then
    pcall(watcher.close, watcher)
    return
  end

  buffer_state.watcher = watcher
  buffer_state.watcher_path = path
end

local function remember_snapshot(bufnr)
  if not is_normal_file_buffer(bufnr) then
    return
  end

  state[bufnr] = state[bufnr] or {}
  state[bufnr].snapshot_lines = vim.api.nvim_buf_get_lines(bufnr, 0, -1, false)
  state[bufnr].snapshot_name = vim.api.nvim_buf_get_name(bufnr)
  state[bufnr].snapshot_stat = file_stat(state[bufnr].snapshot_name)
  ensure_file_watcher(bufnr)
end

local function next_generation(bufnr)
  vim.b[bufnr].fancy_reopen_generation = (vim.b[bufnr].fancy_reopen_generation or 0) + 1
  return vim.b[bufnr].fancy_reopen_generation
end

local function current_generation(bufnr)
  return vim.b[bufnr].fancy_reopen_generation or 0
end

local function register_visual_mode(name, mode)
  visual_modes[name] = vim.tbl_extend("force", mode or {}, { name = name })
end

local function resolve_visual_mode(bufnr)
  local configured_name = vim.b[bufnr].fancy_reopen_diff_mode
    or vim.g.fancy_reopen_diff_mode
    or default_visual_mode

  return visual_modes[configured_name] or visual_modes[default_visual_mode]
end

local function normalize_hunks(before_lines, after_lines, hunks)
  local normalized = {}

  for _, hunk in ipairs(hunks) do
    local old_start = hunk[1]
    local old_count = hunk[2]
    local new_start = hunk[3]
    local new_count = hunk[4]
    local prefix = 0
    local suffix = 0

    while prefix < old_count
      and prefix < new_count
      and before_lines[old_start + prefix] == after_lines[new_start + prefix]
    do
      prefix = prefix + 1
    end

    while suffix < (old_count - prefix)
      and suffix < (new_count - prefix)
      and before_lines[old_start + old_count - suffix - 1] == after_lines[new_start + new_count - suffix - 1]
    do
      suffix = suffix + 1
    end

    local trimmed_old_count = old_count - prefix - suffix
    local trimmed_new_count = new_count - prefix - suffix

    if trimmed_old_count > 0 or trimmed_new_count > 0 then
      normalized[#normalized + 1] = {
        old_start + prefix,
        trimmed_old_count,
        new_start + prefix,
        trimmed_new_count,
      }
    end
  end

  return normalized
end

local function largest_hunk_target(hunks)
  local best_size = -1
  local best_line = 1

  for _, hunk in ipairs(hunks) do
    local old_count = hunk[2]
    local new_start = hunk[3]
    local new_count = hunk[4]
    local size = math.max(old_count, new_count)
    local span = math.max(new_count, 1)
    local target_line = new_start + math.floor((span - 1) / 2)

    if size > best_size then
      best_size = size
      best_line = math.max(target_line, 1)
    end
  end

  return best_line
end

local function analyze_diff(before_lines, after_lines)
  local raw_hunks = vim.diff(joined(before_lines), joined(after_lines), {
    result_type = "indices",
    algorithm = "histogram",
    linematch = 160,
    ctxlen = 0,
    interhunkctxlen = 0,
  })

  if not raw_hunks or vim.tbl_isempty(raw_hunks) then
    return nil
  end

  local hunks = normalize_hunks(before_lines, after_lines, raw_hunks)
  if vim.tbl_isempty(hunks) then
    return nil
  end

  return hunks
end

local function center_windows_on_line(bufnr, line)
  local wins = vim.fn.win_findbuf(bufnr)
  local line_count = vim.api.nvim_buf_line_count(bufnr)

  for _, winid in ipairs(wins) do
    if vim.api.nvim_win_is_valid(winid) then
      vim.api.nvim_win_call(winid, function()
        local view = vim.fn.winsaveview()
        local win_height = vim.api.nvim_win_get_height(winid)
        local max_topline = math.max(line_count - win_height + 1, 1)
        local topline = math.max(math.min(line - math.floor(win_height / 2), max_topline), 1)

        view.topline = topline
        view.lnum = math.max(math.min(line, line_count), 1)
        view.col = 0
        view.curswant = 0
        vim.fn.winrestview(view)
      end)
    end
  end
end

local function visible_windows_for_buffer(bufnr)
  local wins = vim.fn.win_findbuf(bufnr)
  local line_count = vim.api.nvim_buf_line_count(bufnr)
  local windows = {}

  for _, winid in ipairs(wins) do
    if vim.api.nvim_win_is_valid(winid) then
      local info = vim.fn.getwininfo(winid)[1]
      if info then
        local top = math.max(info.topline or 1, 1)
        local bottom = math.min(info.botline or top, line_count)
        windows[#windows + 1] = {
          winid = winid,
          top = top,
          bottom = bottom,
          width = math.max(vim.api.nvim_win_get_width(winid), 1),
          height = math.max(bottom - top + 1, 1),
        }
      end
    end
  end

  if vim.tbl_isempty(windows) then
    local cursor = math.max(vim.api.nvim_win_get_cursor(0)[1], 1)
    windows[1] = {
      winid = vim.api.nvim_get_current_win(),
      top = cursor,
      bottom = math.min(cursor + math.max(vim.api.nvim_win_get_height(0) - 1, 0), line_count),
      width = math.max(vim.api.nvim_win_get_width(0), 1),
    }
    windows[1].height = math.max(windows[1].bottom - windows[1].top + 1, 1)
  end

  return windows
end

local function render_deleted_hunks(bufnr, before_lines, hunks)
  local line_count = vim.api.nvim_buf_line_count(bufnr)
  local last_row = math.max(line_count - 1, 0)

  vim.api.nvim_buf_clear_namespace(bufnr, delete_namespace, 0, -1)

  for _, hunk in ipairs(hunks) do
    local old_start = hunk[1]
    local old_count = hunk[2]
    local new_start = hunk[3]
    local new_count = hunk[4]
    local row = math.min(math.max(new_start - 1, 0), last_row)

    if old_count > 0 and new_count == 0 then
      local virt_lines = {}
      local capped = math.min(old_count, 6)

      for offset = 0, capped - 1 do
        local old_line = before_lines[old_start + offset] or ""
        virt_lines[#virt_lines + 1] = {
          { "  - ", "FancyReopenDiffDeletePrefix" },
          { old_line, "FancyReopenDiffDeleteInline" },
        }
      end

      if old_count > capped then
        virt_lines[#virt_lines + 1] = {
          { string.format("  - ... %d more line(s)", old_count - capped), "FancyReopenDiffDeletePrefix" },
        }
      end

      vim.api.nvim_buf_set_extmark(bufnr, delete_namespace, row, 0, {
        virt_lines = virt_lines,
        virt_lines_above = true,
        priority = 260,
      })
    end
  end
end

local function render_deleted_morph_hunks(bufnr, before_lines, hunks)
  local line_count = vim.api.nvim_buf_line_count(bufnr)
  local last_row = math.max(line_count - 1, 0)

  vim.api.nvim_buf_clear_namespace(bufnr, delete_namespace, 0, -1)

  for _, hunk in ipairs(hunks) do
    local old_start = hunk[1]
    local old_count = hunk[2]
    local new_start = hunk[3]
    local new_count = hunk[4]
    local row = math.min(math.max(new_start - 1, 0), last_row)

    if old_count > 0 and new_count == 0 then
      local virt_lines = {}
      local capped = math.min(old_count, 6)

      for offset = 0, capped - 1 do
        local old_line = before_lines[old_start + offset] or ""
        local noise = glitch_char((old_start + offset) * 17, offset + 1)
        virt_lines[#virt_lines + 1] = {
          { "  ⨯ ", "FancyReopenDiffMorphDeleteMid" },
          { noise .. " ", "FancyReopenDiffMorphDeleteHot" },
          { old_line, "FancyReopenDiffMorphDeleteGhost" },
        }
      end

      if old_count > capped then
        virt_lines[#virt_lines + 1] = {
          { string.format("  ⨯ ... %d more line(s)", old_count - capped), "FancyReopenDiffMorphDeleteMid" },
        }
      end

      vim.api.nvim_buf_set_extmark(bufnr, delete_namespace, row, 0, {
        virt_lines = virt_lines,
        virt_lines_above = true,
        priority = 260,
      })
    end
  end
end

local function build_change_segments(bufnr, hunks)
  local segments = {}
  local line_count = vim.api.nvim_buf_line_count(bufnr)

  for _, hunk in ipairs(hunks) do
    local old_count = hunk[2]
    local new_start = hunk[3]
    local new_count = hunk[4]

    if new_count > 0 then
      local start_row = math.max(new_start - 1, 0)
      local end_row = math.min(start_row + new_count - 1, math.max(line_count - 1, 0))

      if start_row <= end_row then
        local span = end_row - start_row + 1
        local center_low = start_row + math.floor((span - 1) / 2)
        local center_high = start_row + math.floor(span / 2)

        segments[#segments + 1] = {
          kind = old_count > 0 and "change" or "add",
          start_row = start_row,
          end_row = end_row,
          center_low = center_low,
          center_high = center_high,
          max_distance = math.max(center_low - start_row, end_row - center_high),
        }
      end
    end
  end

  return segments
end

local function changed_line_count(hunks)
  local total = 0

  for _, hunk in ipairs(hunks) do
    total = total + math.max(hunk[2], hunk[4])
  end

  return total
end

local function should_skip_animation_for_buffer(bufnr, before_lines, after_lines)
  if math.max(#before_lines, #after_lines) > max_animated_file_lines then
    return true
  end

  local buffer_state = state[bufnr]
  local before_stat = buffer_state and buffer_state.snapshot_stat or nil
  local current_name = vim.api.nvim_buf_get_name(bufnr)
  local after_stat = file_stat(current_name)

  if (before_stat and before_stat.size and before_stat.size > max_animated_file_bytes)
    or (after_stat and after_stat.size and after_stat.size > max_animated_file_bytes)
  then
    return true
  end

  return false
end

local function should_skip_animation_for_hunks(hunks)
  return changed_line_count(hunks) > max_animated_changed_lines
end

local function segment_highlights(segment, is_core)
  if segment.kind == "change" then
    return is_core and "FancyReopenDiffChangeCore" or "FancyReopenDiffChangeTrail", "FancyReopenDiffChangeSign"
  end

  return is_core and "FancyReopenDiffAddCore" or "FancyReopenDiffAddTrail", "FancyReopenDiffAddSign"
end

local function render_segment_row(bufnr, segment, row, is_core)
  local line_hl_group, sign_hl_group = segment_highlights(segment, is_core)

  vim.api.nvim_buf_set_extmark(bufnr, add_namespace, row, 0, {
    priority = 250,
    sign_text = "▎",
    sign_hl_group = sign_hl_group,
    line_hl_group = line_hl_group,
    hl_eol = true,
  })
end

local function segment_row_distance(segment, row)
  if row >= segment.center_low and row <= segment.center_high then
    return 0
  end

  return math.min(math.abs(row - segment.center_low), math.abs(row - segment.center_high))
end

local function render_change_frame(bufnr, segments, cleared_distance)
  vim.api.nvim_buf_clear_namespace(bufnr, add_namespace, 0, -1)

  for _, segment in ipairs(segments) do
    for row = segment.start_row, segment.end_row do
      local distance = segment_row_distance(segment, row)
      if distance > cleared_distance then
        render_segment_row(bufnr, segment, row, distance == 0)
      end
    end
  end
end

local function random_corner()
  local corners = {
    { row = 0, col = 0 },
    { row = 0, col = 1 },
    { row = 1, col = 0 },
    { row = 1, col = 1 },
  }

  return corners[math.random(#corners)]
end

local function ease_out_cubic(t)
  return 1 - ((1 - t) ^ 3)
end

local matrix_glyphs = { "0", "1", "#", "@", "%", "&", "$", "=", "+", "*", "/", "\\", "|", "~", "^" }

local function line_char_at(line, col)
  if not line or line == "" then
    return " "
  end

  local char = vim.fn.strcharpart(line, col, 1)
  return char ~= "" and char or " "
end

glitch_char = function(seed, frame)
  return matrix_glyphs[((seed + (frame * 7)) % #matrix_glyphs) + 1]
end

local function row_change_kinds(hunks)
  local kinds = {}

  for _, hunk in ipairs(hunks) do
    local old_count = hunk[2]
    local new_start = hunk[3]
    local new_count = hunk[4]
    local kind = old_count == 0 and "add" or "change"

    for offset = 0, math.max(new_count - 1, 0) do
      kinds[new_start + offset - 1] = kind
    end
  end

  return kinds
end

local function render_overlay_rows(bufnr, rows, priority)
  vim.api.nvim_buf_clear_namespace(bufnr, add_namespace, 0, -1)

  for row, overlay in pairs(rows) do
    local has_chunks = overlay.chunks and #overlay.chunks > 0
    local has_text = overlay.text and overlay.text:find("%S")

    if has_chunks or has_text then
      vim.api.nvim_buf_set_extmark(bufnr, add_namespace, row, 0, {
        priority = priority or 250,
        virt_text = overlay.chunks or { { overlay.text, overlay.hl_group } },
        virt_text_pos = "overlay",
        hl_mode = "replace",
      })
    end
  end
end

local function render_overlay_fragments(bufnr, rows, priority)
  vim.api.nvim_buf_clear_namespace(bufnr, add_namespace, 0, -1)

  for row, fragments in pairs(rows) do
    for _, fragment in ipairs(fragments) do
      if fragment.text and fragment.text:find("%S") then
        vim.api.nvim_buf_set_extmark(bufnr, add_namespace, row, 0, {
          priority = priority or 250,
          virt_text = { { fragment.text, fragment.hl_group } },
          virt_text_pos = "overlay",
          virt_text_win_col = fragment.col,
          hl_mode = "combine",
        })
      end
    end
  end
end

register_visual_mode("git_hunks", {
  prepare = function(bufnr, context)
    context.change_segments = build_change_segments(bufnr, context.hunks)
    context.max_distance = 0
    context.total_frames = git_hunks_frame_count

    for _, segment in ipairs(context.change_segments) do
      context.max_distance = math.max(context.max_distance, segment.max_distance)
    end
  end,
  start = function(bufnr, context)
    render_deleted_hunks(bufnr, context.before_lines, context.hunks)
  end,
  frame = function(bufnr, context)
    if vim.tbl_isempty(context.change_segments) then
      return true
    end

    local frame_index = context.distance + 1
    local progress = math.min(frame_index / context.total_frames, 1)
    local cleared_distance = math.floor(progress * (context.max_distance + 2)) - 1

    render_change_frame(bufnr, context.change_segments, cleared_distance)
    context.distance = frame_index
    return frame_index >= context.total_frames
  end,
  finish_delay = function(_, context)
    if vim.tbl_isempty(context.change_segments) then
      return animation_start_delay_ms + 420
    end

    return animation_start_delay_ms
  end,
})

register_visual_mode("digital_wipe", {
  prepare = function(bufnr, context)
    context.windows = visible_windows_for_buffer(bufnr)
    context.reveal_frames = digital_wipe_reveal_frames
    context.fade_frames = digital_wipe_fade_frames
    context.total_frames = context.reveal_frames + context.fade_frames
    context.cells = {}
    context.row_kinds = row_change_kinds(context.hunks)

    for _, win in ipairs(context.windows) do
      local corner = random_corner()
      local max_row_distance = math.max(win.height - 1, 0)
      local max_col_distance = math.max(win.width - 1, 0)
      local row_origin = corner.row == 0 and 0 or max_row_distance
      local col_origin = corner.col == 0 and 0 or max_col_distance
      local max_distance = math.max(max_row_distance + max_col_distance, 1)
      local row_freq = 1.4 + ((win.winid % 5) * 0.21)
      local col_freq = 1.2 + ((win.winid % 7) * 0.17)
      local swirl = 0.07 + ((win.winid % 4) * 0.018)
      local band_shift = ((win.winid % 13) / 13)

      for line = win.top, win.bottom do
        local relative_row = line - win.top
        local before_line = context.before_lines[line] or ""
        local after_line = context.after_lines[line] or ""

        for col = 0, win.width - 1 do
          local row_ratio = win.height > 1 and (relative_row / (win.height - 1)) or 0
          local col_ratio = win.width > 1 and (col / (win.width - 1)) or 0
          local normalized_distance = (math.abs(relative_row - row_origin) + math.abs(col - col_origin)) / max_distance
          local seed = ((win.winid * 29) + (line * 37) + (col * 17)) % 997
          local jitter = (((seed % 31) / 31) - 0.5) * 0.22
          local wave = math.sin((row_ratio * math.pi * row_freq) + (col_ratio * math.pi * 0.9) + band_shift)
          local cross_wave = math.cos((col_ratio * math.pi * col_freq) - (row_ratio * math.pi * 1.3) + band_shift)
          local swirl_offset = (row_ratio - col_ratio) * swirl
          local flow = (wave * 0.11) + (cross_wave * 0.08) + swirl_offset
          local band = (math.sin((row_ratio * 19) + (col_ratio * 11) + band_shift) + 1) * 0.5
          local before_char = line_char_at(before_line, col)
          local after_char = line_char_at(after_line, col)

          context.cells[#context.cells + 1] = {
            row = line - 1,
            col = col,
            row_ratio = row_ratio,
            col_ratio = col_ratio,
            distance = math.max(0, math.min(normalized_distance + jitter + flow, 1)),
            seed = seed,
            band = band,
            before_char = before_char,
            after_char = after_char,
            changed = before_char ~= after_char,
            row_kind = context.row_kinds[line - 1],
          }
        end
      end
    end
  end,
  start = function(bufnr, context)
    center_windows_on_line(bufnr, context.target_line)
    render_deleted_morph_hunks(bufnr, context.before_lines, context.hunks)
  end,
  frame = function(bufnr, context)
    local rows = {}
    local frame = context.distance + 1
    local reveal_progress = ease_out_cubic(math.min(frame / context.reveal_frames, 1))
    local fade_progress = context.fade_frames > 0
        and math.min(math.max(frame - context.reveal_frames, 0) / context.fade_frames, 1)
      or 1
    local frame_phase = frame / context.total_frames

    for _, cell in ipairs(context.cells) do
      local frame_wave = (math.sin((cell.row_ratio * 13) + (cell.col_ratio * 17) + (frame_phase * math.pi * 6)) + 1) * 0.5
      local threshold = reveal_progress + ((frame_wave - 0.5) * 0.14)
      local edge_delta = threshold - cell.distance
      local morph_progress = math.max(0, math.min((edge_delta + 0.18) / 0.36, 1))
      local settled_progress = math.max(0, math.min((morph_progress - 0.55) / 0.45, 1))
      local noise = ((cell.seed + frame * 11) % 100) / 100
      local scan = (math.sin((cell.row_ratio * 26) - (frame_phase * math.pi * 8)) + 1) * 0.5
      local glitch = ((cell.seed + frame * 23) % 157) / 157
      local char
      local hl_group

      if morph_progress <= 0 and not (cell.changed and glitch > 0.985 and reveal_progress > cell.distance - 0.08) then
        goto continue
      end

      if cell.changed then
        local is_deleteish = cell.before_char ~= " " and cell.after_char == " "
        local dense_hl = is_deleteish and "FancyReopenDiffMorphDeleteHot" or "FancyReopenDiffWipeDense"
        local mid_hl = is_deleteish and "FancyReopenDiffMorphDeleteMid" or "FancyReopenDiffWipeMid"
        local trace_hl = is_deleteish and "FancyReopenDiffMorphDeleteMid" or "FancyReopenDiffWipeTrace"
        local ghost_hl = is_deleteish and "FancyReopenDiffMorphDeleteGhost" or "FancyReopenDiffWipeGhost"
        local hot_hl = is_deleteish and "FancyReopenDiffMorphDeleteHot" or "FancyReopenDiffWipeHot"

        if morph_progress < 0.24 then
          char = cell.before_char ~= " " and cell.before_char or glitch_char(cell.seed, frame)
          hl_group = scan > 0.58 and trace_hl or ghost_hl
        elseif morph_progress < 0.58 then
          char = glitch > 0.52 and glitch_char(cell.seed, frame)
            or (noise > 0.38 and cell.before_char or cell.after_char)
          hl_group = glitch > 0.93 and hot_hl
            or (noise > 0.42 and mid_hl or trace_hl)
        elseif morph_progress < 0.86 then
          char = glitch > 0.38 and glitch_char(cell.seed, frame)
            or (noise > 0.18 and cell.after_char or cell.before_char)
          hl_group = glitch > 0.92 and hot_hl
            or (scan > 0.72 and trace_hl or dense_hl)
        elseif settled_progress < fade_progress then
          char = glitch > 0.84 and glitch_char(cell.seed, frame) or cell.after_char
          hl_group = glitch > 0.93 and hot_hl
            or (cell.band > 0.55 and dense_hl or mid_hl)
        elseif glitch > 0.93 then
          char = glitch_char(cell.seed, frame)
          hl_group = trace_hl
        end
      else
        if cell.after_char ~= " " and morph_progress < 0.14 and glitch > 0.94 then
          char = glitch_char(cell.seed, frame)
          hl_group = "FancyReopenDiffWipeGhost"
        elseif cell.after_char ~= " " and morph_progress >= 0.16 and morph_progress < 0.32 and scan > 0.93 and glitch > 0.82 then
          char = glitch_char(cell.seed, frame)
          hl_group = "FancyReopenDiffWipeTrace"
        elseif cell.after_char ~= " " and morph_progress >= 0.32 and glitch > 0.985 and fade_progress < 0.55 then
          char = "·"
          hl_group = "FancyReopenDiffWipeGhost"
        end

        if not char then
          goto continue
        end
      end

      if char and char ~= " " then
        rows[cell.row] = rows[cell.row] or {}
        rows[cell.row][#rows[cell.row] + 1] = {
          col = cell.col,
          text = char,
          hl_group = hl_group,
        }
      end

      ::continue::
    end

    local rendered_rows = {}
    for row, fragments in pairs(rows) do
      table.sort(fragments, function(a, b)
        return a.col < b.col
      end)

      local merged = {}
      local current = nil

      for _, fragment in ipairs(fragments) do
        if current and current.hl_group == fragment.hl_group and current.col + #current.text == fragment.col then
          current.text = current.text .. fragment.text
        else
          current = {
            col = fragment.col,
            text = fragment.text,
            hl_group = fragment.hl_group,
          }
          merged[#merged + 1] = current
        end
      end

      rendered_rows[row] = merged
    end

    render_overlay_fragments(bufnr, rendered_rows, 255)
    context.distance = frame
    return frame >= context.total_frames
  end,
  finish_delay = function()
    return math.max(math.floor(animation_start_delay_ms * 0.4), 80)
  end,
})

local function finish_animation(bufnr, generation, restore_modifiable)
  if current_generation(bufnr) ~= generation then
    return
  end

  stop_animation_timer(bufnr)
  clear_animation(bufnr)

  if vim.api.nvim_buf_is_valid(bufnr) then
    vim.bo[bufnr].modifiable = restore_modifiable
  end

  state[bufnr] = state[bufnr] or {}
  state[bufnr].restore_modifiable = nil
end

local function cancel_animation(bufnr)
  next_generation(bufnr)
  stop_animation_timer(bufnr)
  clear_animation(bufnr)
  restore_buffer_modifiable(bufnr)
end

local function cleanup_all_runtime_state()
  for bufnr, _ in pairs(state) do
    stop_animation_timer(bufnr)
    stop_file_watcher(bufnr)
    clear_animation(bufnr)
    restore_buffer_modifiable(bufnr)
  end
end

play_animation = function(bufnr, before_lines, after_lines)
  restore_buffer_modifiable(bufnr)
  local generation = next_generation(bufnr)
  stop_animation_timer(bufnr)
  clear_animation(bufnr)

  if same_lines(before_lines, after_lines) then
    return
  end

  if should_skip_animation_for_buffer(bufnr, before_lines, after_lines) then
    return
  end

  local hunks = analyze_diff(before_lines, after_lines)
  if not hunks then
    return
  end

  if should_skip_animation_for_hunks(hunks) then
    return
  end

  local mode = resolve_visual_mode(bufnr)
  local target_line = largest_hunk_target(hunks)
  local restore_modifiable = vim.bo[bufnr].modifiable
  local context = {
    bufnr = bufnr,
    before_lines = before_lines,
    after_lines = after_lines,
    hunks = hunks,
    target_line = target_line,
    distance = -1,
  }

  state[bufnr] = state[bufnr] or {}
  state[bufnr].restore_modifiable = restore_modifiable
  state[bufnr].active_mode = mode.name

  vim.bo[bufnr].modifiable = false
  mode.prepare(bufnr, context)
  mode.start(bufnr, context)

  local finish_delay = mode.finish_delay and mode.finish_delay(bufnr, context) or animation_start_delay_ms

  if mode.is_static or (context.change_segments and vim.tbl_isempty(context.change_segments)) then
    local timer = uv.new_timer()
    if not timer then
      finish_animation(bufnr, generation, restore_modifiable)
      return
    end

    state[bufnr].timer = timer
    timer:start(finish_delay, 0, vim.schedule_wrap(function()
      finish_animation(bufnr, generation, restore_modifiable)
    end))
    return
  end

  local timer = uv.new_timer()
  if not timer then
    finish_animation(bufnr, generation, restore_modifiable)
    return
  end

  state[bufnr].timer = timer
  local function tick()
    if current_generation(bufnr) ~= generation or not vim.api.nvim_buf_is_valid(bufnr) then
      stop_animation_timer(bufnr)
      return
    end

    if mode.frame(bufnr, context) then
      finish_animation(bufnr, generation, restore_modifiable)
      return
    end

    timer:stop()
    timer:start(animation_step_ms, 0, vim.schedule_wrap(tick))
  end

  timer:start(animation_start_delay_ms, 0, vim.schedule_wrap(tick))
end

local function maybe_animate_reload(bufnr, request_id, retries_left)
  if not is_normal_file_buffer(bufnr) then
    return
  end

  local current_name = vim.api.nvim_buf_get_name(bufnr)
  local after_lines = vim.api.nvim_buf_get_lines(bufnr, 0, -1, false)
  local buffer_state = state[bufnr]
  local before_stat = buffer_state and buffer_state.snapshot_stat or nil
  local after_stat = file_stat(current_name)

  if buffer_state and request_id and buffer_state.reload_request_id ~= request_id then
    return
  end

  vim.b[bufnr].fancy_reopen_loaded_once = true

  if not buffer_state or not buffer_state.snapshot_lines or buffer_state.snapshot_name ~= current_name then
    cancel_animation(bufnr)
    remember_snapshot(bufnr)
    return
  end

  local before_lines = buffer_state.snapshot_lines

  if same_lines(before_lines, after_lines) then
    if retries_left > 0 and not same_file_stat(before_stat, after_stat) then
      vim.defer_fn(function()
        local current_state = state[bufnr]
        if current_state and current_state.reload_request_id == request_id then
          maybe_animate_reload(bufnr, request_id, retries_left - 1)
        end
      end, reload_settle_delay_ms)
      return
    end

    cancel_animation(bufnr)
    remember_snapshot(bufnr)
    return
  end

  vim.schedule(function()
    if vim.api.nvim_buf_is_valid(bufnr) then
      play_animation(bufnr, before_lines, after_lines)
      remember_snapshot(bufnr)
    end
  end)
end

local function queue_reload_animation(bufnr)
  state[bufnr] = state[bufnr] or {}
  state[bufnr].reload_request_id = (state[bufnr].reload_request_id or 0) + 1

  local request_id = state[bufnr].reload_request_id
  vim.defer_fn(function()
    local buffer_state = state[bufnr]
    if buffer_state and buffer_state.reload_request_id == request_id then
      maybe_animate_reload(bufnr, request_id, reload_retry_count)
    end
  end, reload_settle_delay_ms)
end

local function build_modified_test_line(line)
  if line == "" then
    return "Fancy reopen diff modified line"
  end

  return line .. " -- modified"
end

local function test_reopen_animation(opts)
  local bufnr = vim.api.nvim_get_current_buf()
  opts = opts or {}

  if not is_normal_file_buffer(bufnr) then
    vim.notify("Fancy reopen diff test requires a normal file buffer", vim.log.levels.WARN)
    return
  end

  local after_lines = vim.api.nvim_buf_get_lines(bufnr, 0, -1, false)
  local before_lines = vim.deepcopy(after_lines)
  local cursor_line = vim.api.nvim_win_get_cursor(0)[1]
  local has_range = opts.range and opts.line1 and opts.line2 and opts.line2 >= opts.line1
  local input = vim.split(vim.trim(opts.args or ""), "%s+", { trimempty = true })
  local mode = ""
  local visual_mode = nil

  for _, token in ipairs(input) do
    if visual_modes[token] then
      visual_mode = token
    elseif mode == "" then
      mode = token
    end
  end

  if mode == "" then
    mode = has_range and "change" or "add"
  end

  if #after_lines == 0 then
    before_lines = {}
    after_lines = { "Fancy reopen diff test line" }
    vim.api.nvim_buf_set_lines(bufnr, 0, -1, false, after_lines)
  elseif has_range and mode == "change" then
    local start_idx = math.max(opts.line1, 1)
    local end_idx = math.min(opts.line2, #before_lines)

    if start_idx > end_idx then
      vim.notify("Fancy reopen diff test range is empty", vim.log.levels.WARN)
      return
    end

    for i = start_idx, end_idx do
      before_lines[i] = build_modified_test_line(before_lines[i])
    end
  elseif has_range then
    local start_idx = math.max(opts.line1, 1)
    local end_idx = math.min(opts.line2, #before_lines)

    if start_idx > end_idx then
      vim.notify("Fancy reopen diff test range is empty", vim.log.levels.WARN)
      return
    end

    for _ = start_idx, end_idx do
      table.remove(before_lines, start_idx)
    end
  elseif mode == "change" then
    before_lines[math.min(cursor_line, #before_lines)] = build_modified_test_line(before_lines[math.min(cursor_line, #before_lines)])
  elseif #before_lines == 1 then
    before_lines[1] = ""
  else
    table.remove(before_lines, math.min(cursor_line, #before_lines))
  end

  local previous_visual_mode = vim.b[bufnr].fancy_reopen_diff_mode
  if visual_mode then
    vim.b[bufnr].fancy_reopen_diff_mode = visual_mode
  end

  play_animation(bufnr, before_lines, after_lines)

  vim.b[bufnr].fancy_reopen_diff_mode = previous_visual_mode
end

vim.api.nvim_create_autocmd("BufReadPost", {
  group = vim.api.nvim_create_augroup("FancyReopenDiff", { clear = true }),
  callback = function(args)
    if not is_normal_file_buffer(args.buf) then
      return
    end

    if vim.b[args.buf].fancy_reopen_loaded_once then
      queue_reload_animation(args.buf)
      return
    end

    vim.b[args.buf].fancy_reopen_loaded_once = true
    remember_snapshot(args.buf)
  end,
})

vim.api.nvim_create_autocmd("BufWritePost", {
  group = "FancyReopenDiff",
  callback = function(args)
    vim.schedule(function()
      if vim.api.nvim_buf_is_valid(args.buf) then
        remember_snapshot(args.buf)
      end
    end)
  end,
})

vim.api.nvim_create_autocmd("BufEnter", {
  group = "FancyReopenDiff",
  callback = function(args)
    if not is_normal_file_buffer(args.buf) then
      return
    end

    vim.b[args.buf].fancy_reopen_loaded_once = true

    local buffer_state = state[args.buf]
    local current_name = vim.api.nvim_buf_get_name(args.buf)
    local current_stat = file_stat(current_name)

    if not buffer_state or not buffer_state.snapshot_lines or buffer_state.snapshot_name ~= current_name then
      remember_snapshot(args.buf)
    else
      ensure_file_watcher(args.buf)

      if not vim.bo[args.buf].modified and buffer_state.snapshot_stat and current_stat
        and not same_file_stat(buffer_state.snapshot_stat, current_stat)
      then
        schedule_checktime(args.buf, 0)
      end
    end
  end,
})

vim.api.nvim_create_autocmd({ "BufHidden", "BufLeave" }, {
  group = "FancyReopenDiff",
  callback = function(args)
    if not is_buffer_visible(args.buf) then
      stop_file_watcher(args.buf)
    end
  end,
})

vim.api.nvim_create_autocmd("FileChangedShellPost", {
  group = "FancyReopenDiff",
  callback = function(args)
    queue_reload_animation(args.buf)
  end,
})

vim.api.nvim_create_autocmd({ "BufDelete", "BufWipeout" }, {
  group = "FancyReopenDiff",
  callback = function(args)
    stop_animation_timer(args.buf)
    stop_file_watcher(args.buf)
    state[args.buf] = nil
  end,
})

pcall(vim.api.nvim_del_user_command, "FancyReopenDiffTest")
vim.api.nvim_create_user_command("FancyReopenDiffTest", function(opts)
  test_reopen_animation(opts)
end, {
  nargs = "?",
  range = true,
  desc = "Preview the fancy reopen diff animation in the current buffer (add/change and optional visual mode)",
})

vim._fancy_reopen_diff_runtime = {
  cleanup_all = cleanup_all_runtime_state,
}
