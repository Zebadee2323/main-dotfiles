local add_namespace = vim.api.nvim_create_namespace("fancy_reopen_diff_add")
local delete_namespace = vim.api.nvim_create_namespace("fancy_reopen_diff_delete")
local state = {}
local uv = vim.uv or vim.loop
local play_animation
local max_animated_file_lines = 5000
local max_animated_file_bytes = 512 * 1024
local max_animated_changed_lines = 200
local animation_start_delay_ms = 300
local animation_step_ms = 70
local reload_settle_delay_ms = 30
local reload_retry_count = 6

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
end

set_highlights()

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

local function ensure_file_watcher(bufnr)
  if not is_normal_file_buffer(bufnr) then
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
      if not is_normal_file_buffer(bufnr) or vim.bo[bufnr].modified then
        return
      end

      local current_path = vim.api.nvim_buf_get_name(bufnr)
      if current_path == "" then
        return
      end

      vim.cmd(string.format("silent! checktime %d", bufnr))
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

local function render_deleted_hunks(bufnr, before_lines, hunks)
  local line_count = vim.api.nvim_buf_line_count(bufnr)
  local last_row = math.max(line_count - 1, 0)

  vim.api.nvim_buf_clear_namespace(bufnr, delete_namespace, 0, -1)

  for _, hunk in ipairs(hunks) do
    local old_start = hunk[1]
    local old_count = hunk[2]
    local new_start = hunk[3]
    local row = math.min(math.max(new_start - 1, 0), last_row)

    if old_count > 0 then
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

local function should_skip_animation(bufnr, before_lines, after_lines, hunks)
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

  local buffer_state = state[bufnr]
  if buffer_state and buffer_state.restore_modifiable ~= nil and vim.api.nvim_buf_is_valid(bufnr) then
    vim.bo[bufnr].modifiable = buffer_state.restore_modifiable
    buffer_state.restore_modifiable = nil
  end
end

play_animation = function(bufnr, before_lines, after_lines)
  local generation = next_generation(bufnr)
  stop_animation_timer(bufnr)
  clear_animation(bufnr)

  if same_lines(before_lines, after_lines) then
    return
  end

  local raw_hunks = vim.diff(joined(before_lines), joined(after_lines), {
    result_type = "indices",
    algorithm = "histogram",
    linematch = 160,
    ctxlen = 0,
    interhunkctxlen = 0,
  })

  if not raw_hunks or vim.tbl_isempty(raw_hunks) then
    return
  end

  local hunks = normalize_hunks(before_lines, after_lines, raw_hunks)

  if vim.tbl_isempty(hunks) then
    return
  end

  if should_skip_animation(bufnr, before_lines, after_lines, hunks) then
    return
  end

  local change_segments = build_change_segments(bufnr, hunks)
  local target_line = largest_hunk_target(hunks)
  local restore_modifiable = vim.bo[bufnr].modifiable
  local max_distance = 0
  local distance = -1

  state[bufnr] = state[bufnr] or {}
  state[bufnr].restore_modifiable = restore_modifiable

  for _, segment in ipairs(change_segments) do
    max_distance = math.max(max_distance, segment.max_distance)
  end

  vim.bo[bufnr].modifiable = false
  center_windows_on_line(bufnr, target_line)
  render_deleted_hunks(bufnr, before_lines, hunks)

  if vim.tbl_isempty(change_segments) then
    local timer = uv.new_timer()
    if not timer then
      finish_animation(bufnr, generation, restore_modifiable)
      return
    end

    state[bufnr].timer = timer
    timer:start(animation_start_delay_ms + 420, 0, vim.schedule_wrap(function()
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

    render_change_frame(bufnr, change_segments, distance)
    distance = distance + 1

    if distance > max_distance + 1 then
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
  local mode = vim.trim(opts.args or "")

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

  play_animation(bufnr, before_lines, after_lines)
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

    if not buffer_state or not buffer_state.snapshot_lines or buffer_state.snapshot_name ~= current_name then
      remember_snapshot(args.buf)
    else
      ensure_file_watcher(args.buf)
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

vim.api.nvim_create_user_command("FancyReopenDiffTest", function(opts)
  test_reopen_animation(opts)
end, {
  nargs = "?",
  range = true,
  desc = "Preview the fancy reopen diff animation in the current buffer (add or change)",
})
