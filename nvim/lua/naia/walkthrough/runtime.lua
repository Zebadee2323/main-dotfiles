-- One playback controller per editor. Loaded only by walkthrough.setup().
local M = {}
local uv = vim.uv or vim.loop
local walkthrough_augroup = vim.api.nvim_create_augroup("ai_walkthrough", { clear = true })
local view = require("naia.walkthrough.view")
local set_walkthrough_highlight = view.highlights
local walkthrough_temp_keymaps = {
  { lhs = "<C-Space>", rhs = "<Cmd>AIWalkToggle<CR>", desc = "Toggle AI walkthrough playback" },
  { lhs = "[w", rhs = "<Cmd>AIWalkPrev<CR>", desc = "Replay previous AI walkthrough step" },
  { lhs = "]w", rhs = "<Cmd>AIWalkNext<CR>", desc = "Advance AI walkthrough by one step" },
  { lhs = "<C-j>", rhs = "<Cmd>AIWalkWindow<CR>", desc = "Open AI walkthrough browser" },
  { lhs = "<C-k>", rhs = "<Cmd>AIWalkParent<CR>", desc = "Jump to parent AI walkthrough step" },
}
local saved_walkthrough_keymaps = {}
local walkthrough_keymaps_installed = false

local playback_state = {
  active = false,
  steps = nil,
  index = 0,
  timer = nil,
  waiting_for_voice = false,
  voice_started = false,
  pause_after_step = false,
  paused_for_continue = false,
  auto_pause_at_index = nil,
  focus_state = nil,
  display_state = nil,
  display_window = nil,
  display_window_created = false,
  highlighted_buffer = nil,
  description_buffer = nil,
  description_window = nil,
  browser_buffer = nil,
  browser_window = nil,
}

local last_walkthrough = nil
local next_walkthrough_step_id = 1
local tree = require("naia.walkthrough.tree")
local find_step_index_by_id = tree.find
local collect_descendant_step_ids = tree.descendants
local pause_walkthrough
local browser
local function render_walkthrough_browser_window() return browser.render() end
local rich = require("naia.walkthrough.tools")
local confirmation = require("naia.walkthrough.prompt")
local enhanced

local function unregister_walkthrough_keymaps()
  if not walkthrough_keymaps_installed then return end
  for _, keymap in ipairs(walkthrough_temp_keymaps) do
    pcall(vim.keymap.del, "n", keymap.lhs)

    local saved = saved_walkthrough_keymaps[keymap.lhs]
    if saved then
      pcall(vim.fn.mapset, saved.mode or "n", false, saved)
      saved_walkthrough_keymaps[keymap.lhs] = nil
    end
  end
  walkthrough_keymaps_installed = false
end

local function register_walkthrough_keymaps()
  for _, keymap in ipairs(walkthrough_temp_keymaps) do
    if not saved_walkthrough_keymaps[keymap.lhs] then
      -- We replace global mappings only. maparg() can return a buffer-local
      -- shadow, which must never be restored over another buffer's mappings.
      for _, existing in ipairs(vim.api.nvim_get_keymap("n")) do
        if vim.keycode(existing.lhs) == vim.keycode(keymap.lhs) then
          saved_walkthrough_keymaps[keymap.lhs] = existing
          break
        end
      end
    end

    vim.keymap.set("n", keymap.lhs, keymap.rhs, {
      desc = keymap.desc,
      silent = true,
    })
  end
  walkthrough_keymaps_installed = true
end

local function trim(value)
  if type(value) ~= "string" then
    return ""
  end

  return vim.trim(value)
end

local function get_post_voice_delay_ms()
  local delay_ms = tonumber(vim.g.ai_walkthrough_post_voice_delay_ms)
  if delay_ms ~= nil then
    return math.max(0, math.floor(delay_ms))
  end

  local delay_seconds = tonumber(vim.g.ai_walkthrough_post_voice_delay_seconds)
  if delay_seconds ~= nil then
    return math.max(0, math.floor(delay_seconds * 1000))
  end

  return 500
end

local function get_voice_disabled_extra_delay_ms()
  local delay_ms = tonumber(vim.g.ai_walkthrough_voice_disabled_extra_delay_ms)
  if delay_ms ~= nil then
    return math.max(0, math.floor(delay_ms))
  end

  local delay_seconds = tonumber(vim.g.ai_walkthrough_voice_disabled_extra_delay_seconds)
  if delay_seconds ~= nil then
    return math.max(0, math.floor(delay_seconds * 1000))
  end

  return 3000
end

local function get_inter_step_delay_ms(opts)
  opts = opts or {}

  local delay_ms = get_post_voice_delay_ms()
  if opts.voice_disabled then
    delay_ms = delay_ms + get_voice_disabled_extra_delay_ms()
  end

  return delay_ms
end

local function is_ai_voice_enabled()
  if type(_G.ai_voice_is_enabled) == "function" then
    return _G.ai_voice_is_enabled()
  end

  return vim.g.ai_voice_enabled ~= false
end

local paths = require("naia.walkthrough.paths")
local resolve_step_path = paths.resolve

enhanced = rich.new(playback_state, resolve_step_path)

local function assign_step_identity(step, parent_id)
  if type(step) ~= "table" then
    return step
  end

  if step.id == nil then
    step.id = next_walkthrough_step_id
    next_walkthrough_step_id = next_walkthrough_step_id + 1
  else
    next_walkthrough_step_id = math.max(next_walkthrough_step_id, tonumber(step.id) or 0)
    next_walkthrough_step_id = next_walkthrough_step_id + 1
  end

  if parent_id ~= nil then
    step.parent_id = parent_id
  elseif step.parent_id == nil then
    step.parent_id = nil
  end

  return step
end

local function prepare_walkthrough_steps(steps, parent_id)
  if type(steps) ~= "table" then
    return steps
  end

  local prepared = {}

  for _, step in ipairs(steps) do
    if type(step) == "table" then
      local copied = vim.deepcopy(step)
      assign_step_identity(copied, parent_id)
      table.insert(prepared, copied)
    end
  end

  return prepared
end

local function parse_walkthrough_response(text)
  local response = require("naia.walkthrough.response")
  return response.parse(text, function(decoded) return response.normalize(decoded, resolve_step_path) end)
end

local get_buffer_absolute_path = view.buffer_path
local select_display_window = view.select
local capture_window_state = view.capture
local restore_window_state = view.restore
local presentation = view.new(playback_state, enhanced)
local clear_active_step_highlight = presentation.clear_highlight
local close_walkthrough_description_box = presentation.close_description
local highlight_walkthrough_step = presentation.highlight
local show_walkthrough_description_box = presentation.describe
local focus_walkthrough_range = view.focus

local function stop_timer()
  playback_state.timer_generation = (playback_state.timer_generation or 0) + 1
  if playback_state.timer then
    pcall(playback_state.timer.stop, playback_state.timer)
    pcall(playback_state.timer.close, playback_state.timer)
    playback_state.timer = nil
  end
end

local function invoke_user_command(name, args)
  local ok = pcall(vim.api.nvim_cmd, {
    cmd = name,
    args = args or {},
  }, {})

  return ok
end

local function close_walkthrough_browser_window()
  browser.close()
end

local function reset_playback_state()
  enhanced:clear()
  clear_active_step_highlight()
  close_walkthrough_description_box()
  close_walkthrough_browser_window()
  unregister_walkthrough_keymaps()
  playback_state.active = false
  playback_state.steps = nil
  playback_state.index = 0
  playback_state.waiting_for_voice = false
  playback_state.voice_started = false
  playback_state.voice_request_id = nil
  playback_state.after_narration = nil
  playback_state.pause_after_step = false
  playback_state.paused_for_continue = false
  playback_state.auto_pause_at_index = nil
  playback_state.focus_state = nil
  playback_state.display_state = nil
  playback_state.display_window = nil
  playback_state.display_window_created = false
  playback_state.highlighted_buffer = nil
  playback_state.description_buffer = nil
  playback_state.description_window = nil
  playback_state.browser_buffer = nil
  playback_state.browser_window = nil
end

local function stop_walkthrough(opts)
  opts = opts or {}
  confirmation.close()

  local had_active_walkthrough = playback_state.active or playback_state.timer ~= nil

  stop_timer()

  if opts.stop_voice and (had_active_walkthrough or opts.force_stop_voice) then
    invoke_user_command("AIVoiceStop")
  end

  -- Release the borrowed comparison window before restoring the user's layout.
  enhanced:cleanup_views()

  if opts.restore_display then
    restore_window_state(playback_state.display_state)
  end

  if opts.close_created_window and playback_state.display_window_created and playback_state.display_window then
    pcall(require("naia.walkthrough.layout").without_equalizing, function()
      vim.api.nvim_win_close(playback_state.display_window, false)
    end)
  end

  if opts.restore_focus then
    restore_window_state(playback_state.focus_state)
    if playback_state.focus_state and playback_state.focus_state.window_id and vim.api.nvim_win_is_valid(playback_state.focus_state.window_id) then
      pcall(vim.api.nvim_set_current_win, playback_state.focus_state.window_id)
    end
  end

  reset_playback_state()

  if opts.notify and had_active_walkthrough then
    vim.notify("AI walkthrough stopped", vim.log.levels.INFO)
  elseif opts.notify and not had_active_walkthrough then
    vim.notify("No AI walkthrough is currently running", vim.log.levels.WARN)
  end

  return had_active_walkthrough
end

local function finish_walkthrough()
  playback_state.finished = true
  stop_timer()
  playback_state.waiting_for_voice = false
  playback_state.voice_started = false
  playback_state.pause_after_step = true
  playback_state.paused_for_continue = true
  playback_state.auto_pause_at_index = nil
  vim.notify("AI walkthrough complete", vim.log.levels.INFO)
end

local function ensure_display_window(target_path)
  if playback_state.display_window and vim.api.nvim_win_is_valid(playback_state.display_window) then
    local bufnr = vim.api.nvim_win_get_buf(playback_state.display_window)
    local buf_path = get_buffer_absolute_path(bufnr)

    if buf_path == target_path or not vim.bo[bufnr].modified then
      return playback_state.display_window
    end
  end

  local winid, created = select_display_window(target_path)
  if not winid then
    return nil
  end

  playback_state.display_window = winid
  playback_state.display_window_created = created
  playback_state.display_state = created and nil or capture_window_state(winid)

  return winid
end

local function show_walkthrough_step(step)
  enhanced:cleanup_views()
  close_walkthrough_description_box()
  local resolved, effective = pcall(enhanced.effective, enhanced, step)
  if not resolved then return false, effective end
  step = effective
  local target = step.comparison and step.comparison.right or step
  local winid = step.comparison and playback_state.display_window or nil
  if not (winid and vim.api.nvim_win_is_valid(winid)) then
    winid = ensure_display_window(target.resolved_path)
  end
  if not winid then
    clear_active_step_highlight()
    return false, "AI walkthrough could not find a window to use"
  end

  local ok, err = pcall(function()
    vim.api.nvim_set_current_win(winid)
    enhanced:save_file_header(winid)

    if not step.comparison then
      local current_path = get_buffer_absolute_path(vim.api.nvim_win_get_buf(winid))
      if step.revision or step.text then
        vim.api.nvim_win_set_buf(winid, enhanced:buffer(step))
      elseif current_path ~= step.resolved_path then
        vim.cmd("edit " .. vim.fn.fnameescape(step.resolved_path))
      end
    end

    local focus = enhanced:render(step, winid)
    local bufnr = vim.api.nvim_win_get_buf(winid)
    highlight_walkthrough_step(bufnr, focus.line_start, focus.line_end)
    if step.comparison and step.comparison.diff then
      vim.api.nvim_win_call(winid, function() vim.cmd("diffupdate") end)
    end
    focus_walkthrough_range(winid, focus.line_start, focus.line_end)
    if step.comparison and step.comparison.diff then
      vim.api.nvim_win_call(winid, function()
        -- Synchronize the before pane after native diff-aware centering.
        vim.cmd("syncbind")
      end)
    end
    show_walkthrough_description_box(winid, playback_state.index, focus.line_start, step.description, step)
  end)

  if not ok then
    enhanced:cleanup_views()
    clear_active_step_highlight()
    close_walkthrough_description_box()
    return false, err
  end

  return true
end

local function schedule_next_step(delay_ms, callback)
  stop_timer()
  local generation = playback_state.timer_generation

  local timer = uv.new_timer()
  if not timer then
    vim.schedule(function()
      if generation == playback_state.timer_generation then callback() end
    end)
    return
  end

  playback_state.timer = timer

  timer:start(delay_ms, 0, function()
    timer:stop()
    timer:close()

    vim.schedule(function()
      if generation ~= playback_state.timer_generation then return end
      if playback_state.timer == timer then
        playback_state.timer = nil
      end

      callback()
    end)
  end)
end

local should_pause_after_current_step
local advance_walkthrough_step

local function run_walkthrough_step(index, continue_beat)
  if not playback_state.active or not playback_state.steps then
    return
  end

  local step = playback_state.steps[index]
  if not step then
    finish_walkthrough()
    return
  end

  playback_state.index = index
  playback_state.playback_revision = (playback_state.playback_revision or 0) + 1
  playback_state.finished = false
  if not continue_beat then playback_state.beat_index = 1 end
  playback_state.paused_for_continue = false

  if playback_state.browser_window and vim.api.nvim_win_is_valid(playback_state.browser_window) then
    render_walkthrough_browser_window()
  end

  local shown, err = show_walkthrough_step(step)
  if not shown then
    vim.notify(err, vim.log.levels.WARN)
    playback_state.pause_after_step = true
    playback_state.paused_for_continue = true
    return
  end

  local function after_narration()
    -- A mutation may have inserted steps before this one while narration played.
    if not playback_state.active or not playback_state.steps[playback_state.index]
      or playback_state.steps[playback_state.index].id ~= step.id then return end
    if enhanced:next_beat() then
      schedule_next_step(get_inter_step_delay_ms({ voice_disabled = not is_ai_voice_enabled() }), function()
        run_walkthrough_step(playback_state.index, true)
      end)
    elseif should_pause_after_current_step() then
      playback_state.paused_for_continue = true
      playback_state.finished = playback_state.index == #playback_state.steps
    else
      schedule_next_step(get_inter_step_delay_ms({ voice_disabled = not is_ai_voice_enabled() }), function()
        run_walkthrough_step(playback_state.index + 1)
      end)
    end
  end
  playback_state.after_narration = after_narration

  if not is_ai_voice_enabled() then
    after_narration()
    return
  end

  playback_state.waiting_for_voice = true
  playback_state.voice_started = false
  playback_state.voice_request_id = nil

  if not invoke_user_command("AIVoiceInterrupt", { enhanced:effective(step).description }) then
    playback_state.waiting_for_voice = false
    vim.notify("AI walkthrough could not start voice playback", vim.log.levels.WARN)

    after_narration()
  end
end

should_pause_after_current_step = function()
  if playback_state.pause_after_step then
    return true
  end

  local target_index = tonumber(playback_state.auto_pause_at_index)
  if target_index and playback_state.index >= target_index then
    playback_state.auto_pause_at_index = nil
    playback_state.pause_after_step = true
    return true
  end

  return false
end

local function start_walkthrough(steps, opts)
  opts = opts or {}
  -- Validate the whole tour before disturbing the currently playing one.
  steps = enhanced:normalize(steps)
  steps = prepare_walkthrough_steps(steps)

  if opts.confirm_start then
    confirmation.open(function(accepted)
      if not accepted then return end
      local ok, err = pcall(start_walkthrough, steps, vim.tbl_extend("force", opts, { confirm_start = false }))
      if not ok then vim.notify("AI walkthrough could not start: " .. tostring(err), vim.log.levels.ERROR) end
    end)
    return true, "awaiting_confirmation"
  end

  stop_walkthrough({
    stop_voice = true,
    force_stop_voice = true,
    restore_display = false,
    restore_focus = false,
    close_created_window = false,
    notify = false,
  })

  -- stop clears the previous tour's anchors; establish tracking for every new
  -- location now, including steps that have not been displayed yet.
  steps = enhanced:normalize(steps)
  playback_state.active = true
  enhanced:started()
  playback_state.steps = vim.deepcopy(steps)
  playback_state.index = 0
  playback_state.pause_after_step = opts.pause_after_step == true
  playback_state.paused_for_continue = false
  playback_state.auto_pause_at_index = nil
  playback_state.focus_state = capture_window_state(vim.api.nvim_get_current_win())

  local first_step = playback_state.steps[1]
  local display_window, created = select_display_window(first_step.resolved_path)
  if not display_window then
    reset_playback_state()
    vim.notify("AI walkthrough could not create a display window", vim.log.levels.ERROR)
    return false
  end

  playback_state.display_window = display_window
  playback_state.display_window_created = created
  playback_state.display_state = created and nil or capture_window_state(display_window)
  last_walkthrough = vim.deepcopy(playback_state.steps)
  register_walkthrough_keymaps()

  run_walkthrough_step(1)
  return true
end

local function next_walkthrough_step()
  if not playback_state.active or not playback_state.steps then
    vim.notify("No AI walkthrough is currently running", vim.log.levels.WARN)
    return
  end

  if playback_state.waiting_for_voice then
    advance_walkthrough_step(playback_state.index + 1)
    return
  end

  if playback_state.timer then
    advance_walkthrough_step(playback_state.index + 1)
    return
  end

  if not playback_state.pause_after_step then
    vim.notify("AI walkthrough is not paused for manual continuation", vim.log.levels.WARN)
    return
  end

  if not playback_state.paused_for_continue then
    vim.notify("AI walkthrough is not waiting for continue", vim.log.levels.WARN)
    return
  end

  playback_state.auto_pause_at_index = nil
  advance_walkthrough_step(playback_state.index + 1)
end

local function continue_walkthrough(step_number)
  if not playback_state.active or not playback_state.steps then
    vim.notify("No AI walkthrough is currently running", vim.log.levels.WARN)
    return
  end

  local target_index = nil
  if step_number ~= nil then
    target_index = tonumber(step_number)
    if not target_index then
      vim.notify("AIWalkContinue step must be a number", vim.log.levels.WARN)
      return
    end

    target_index = math.floor(target_index)
    if target_index < 1 then
      vim.notify("AIWalkContinue step must be at least 1", vim.log.levels.WARN)
      return
    end
  end

  if target_index and not playback_state.waiting_for_voice and playback_state.index >= target_index then
    playback_state.pause_after_step = true
    playback_state.paused_for_continue = true
    playback_state.auto_pause_at_index = nil
    vim.notify("AI walkthrough is already at or past that step", vim.log.levels.INFO)
    return
  end

  local was_paused_for_continue = playback_state.paused_for_continue
  playback_state.pause_after_step = false
  playback_state.paused_for_continue = false
  playback_state.auto_pause_at_index = target_index

  if playback_state.waiting_for_voice then
    return
  end

  if playback_state.timer then
    return
  end

  if not was_paused_for_continue then
    return
  end

  stop_timer()
  run_walkthrough_step(playback_state.index + 1)
end

pause_walkthrough = function()
  if not playback_state.active or not playback_state.steps then
    vim.notify("No AI walkthrough is currently running", vim.log.levels.WARN)
    return
  end

  playback_state.pause_after_step = true
  playback_state.auto_pause_at_index = nil

  if playback_state.waiting_for_voice then
    vim.notify("AI walkthrough will pause after the current step", vim.log.levels.INFO)
    return
  end

  stop_timer()
  playback_state.paused_for_continue = true
  vim.notify("AI walkthrough paused", vim.log.levels.INFO)
end

local function toggle_walkthrough_playback()
  if not playback_state.active or not playback_state.steps then
    vim.notify("No AI walkthrough is currently running", vim.log.levels.WARN)
    return
  end

  if playback_state.pause_after_step and playback_state.paused_for_continue and not playback_state.waiting_for_voice then
    continue_walkthrough()
    return
  end

  pause_walkthrough()
end

local function replay_walkthrough_step(index)
  if not playback_state.active or not playback_state.steps then
    vim.notify("No AI walkthrough is currently running", vim.log.levels.WARN)
    return
  end

  local step = playback_state.steps[index]
  if not step then
    vim.notify("No previous AI walkthrough step is available", vim.log.levels.WARN)
    return
  end

  stop_timer()
  playback_state.paused_for_continue = false
  playback_state.auto_pause_at_index = nil
  playback_state.waiting_for_voice = false
  playback_state.voice_started = false
  invoke_user_command("AIVoiceStop")
  run_walkthrough_step(index)
end

advance_walkthrough_step = function(index)
  if not playback_state.active or not playback_state.steps then
    vim.notify("No AI walkthrough is currently running", vim.log.levels.WARN)
    return
  end

  local step = playback_state.steps[index]
  if not step then
    if index > (playback_state.index or 0) then
      finish_walkthrough()
    else
      vim.notify("No AI walkthrough step is available", vim.log.levels.WARN)
    end
    return
  end

  stop_timer()
  playback_state.paused_for_continue = false
  playback_state.auto_pause_at_index = nil
  playback_state.waiting_for_voice = false
  playback_state.voice_started = false
  invoke_user_command("AIVoiceStop")
  run_walkthrough_step(index)
end

local function previous_walkthrough_step()
  if not playback_state.active or not playback_state.steps then
    vim.notify("No AI walkthrough is currently running", vim.log.levels.WARN)
    return
  end

  local previous_index = playback_state.index - 1
  if previous_index < 1 then
    vim.notify("Already at the first AI walkthrough step", vim.log.levels.WARN)
    return
  end

  replay_walkthrough_step(previous_index)
end

local function jump_to_parent_walkthrough_step()
  if not playback_state.active or not playback_state.steps then
    vim.notify("No AI walkthrough is currently running", vim.log.levels.WARN)
    return
  end

  local current_step = playback_state.steps[playback_state.index]
  if not current_step then
    vim.notify("No current AI walkthrough step is available", vim.log.levels.WARN)
    return
  end

  if current_step.parent_id == nil then
    vim.notify("Current AI walkthrough step has no parent", vim.log.levels.INFO)
    return
  end

  local parent_index = find_step_index_by_id(playback_state.steps, current_step.parent_id)
  if not parent_index then
    vim.notify("Parent AI walkthrough step could not be found", vim.log.levels.WARN)
    return
  end

  replay_walkthrough_step(parent_index)
end

local function remove_walkthrough_descendants(anchor_step_id)
  local current_steps = playback_state.steps
  if type(current_steps) ~= "table" or #current_steps == 0 then
    return nil, 0
  end

  local anchor_index = find_step_index_by_id(current_steps, anchor_step_id)
  if not anchor_index then
    return nil, 0
  end

  local descendants = collect_descendant_step_ids(current_steps, anchor_step_id)
  local removed_count = 0
  local remaining = {}

  for _, step in ipairs(current_steps) do
    if descendants[step.id] == true then
      removed_count = removed_count + 1
    else
      table.insert(remaining, vim.deepcopy(step))
    end
  end

  playback_state.steps = remaining
  enhanced:touch()
  last_walkthrough = vim.deepcopy(remaining)

  local updated_anchor_index = find_step_index_by_id(remaining, anchor_step_id)
  return updated_anchor_index, removed_count
end

local function remove_current_walkthrough_enquiry()
  if not playback_state.active or not playback_state.steps then
    vim.notify("No AI walkthrough is currently running", vim.log.levels.WARN)
    return
  end

  local current_step = playback_state.steps[playback_state.index]
  if not current_step then
    vim.notify("No current AI walkthrough step is available", vim.log.levels.WARN)
    return
  end

  local anchor_step_id = current_step.parent_id or current_step.id
  local anchor_index, removed_count = remove_walkthrough_descendants(anchor_step_id)
  if not anchor_index then
    vim.notify("AI walkthrough could not remove the enquiry steps", vim.log.levels.WARN)
    return
  end

  if removed_count == 0 then
    vim.notify("No enquiry steps were attached to that walkthrough step", vim.log.levels.INFO)
  else
    vim.notify(string.format("Removed %d AI walkthrough enquiry step%s", removed_count, removed_count == 1 and "" or "s"), vim.log.levels.INFO)
  end

  replay_walkthrough_step(anchor_index)
end

local function remove_walkthrough_step_ids(step_ids)
  local current_steps = playback_state.steps
  if type(current_steps) ~= "table" or #current_steps == 0 then
    return nil, nil, 0
  end

  local removed_count = 0
  local remaining = {}
  local current_step_id = current_steps[playback_state.index] and current_steps[playback_state.index].id or nil
  local first_removed_index = nil

  for index, step in ipairs(current_steps) do
    if step_ids[step.id] then
      removed_count = removed_count + 1
      if not first_removed_index then
        first_removed_index = index
      end
    else
      table.insert(remaining, vim.deepcopy(step))
    end
  end

  playback_state.steps = remaining
  enhanced:touch()
  last_walkthrough = vim.deepcopy(remaining)

  local next_index = nil
  if current_step_id then
    next_index = find_step_index_by_id(remaining, current_step_id)
  end

  if not next_index and #remaining > 0 then
    next_index = math.min(first_removed_index or #remaining, #remaining)
  end

  return remaining, next_index, removed_count
end

browser = require("naia.walkthrough.browser").new(playback_state, {
  jump = replay_walkthrough_step,
  remove = remove_walkthrough_step_ids,
  interrupt = function()
    stop_timer()
    playback_state.waiting_for_voice = false
    playback_state.voice_started = false
    playback_state.paused_for_continue = false
    playback_state.auto_pause_at_index = nil
    invoke_user_command("AIVoiceStop")
  end,
  stop = function(stop_voice)
    stop_walkthrough({ stop_voice = stop_voice, restore_display = true, restore_focus = true,
      close_created_window = true, notify = true })
  end,
})

local function reset_walkthrough()
  if not playback_state.active or not playback_state.steps or #playback_state.steps == 0 then
    vim.notify("No AI walkthrough is currently running", vim.log.levels.WARN)
    return
  end

  start_walkthrough(playback_state.steps, {
    pause_after_step = playback_state.pause_after_step,
  })
end

local function export_walkthrough_file(args)
  local files = require("naia.walkthrough.file")
  local steps = playback_state.steps or last_walkthrough
  if type(steps) ~= "table" or #steps == 0 then error("No AI walkthrough is available to export", 0) end
  if type(args.path) ~= "string" or trim(args.path) == "" then error("Export requires a file path", 0) end
  local path = vim.fn.fnamemodify(vim.fn.expand(args.path), ":p")
  if (vim.uv or vim.loop).fs_lstat(path) and not args.overwrite then
    error("Walkthrough file already exists; use overwrite=true or AIWalkExport!", 0)
  end
  local text = enhanced:export(steps, files.root(args.root))
  if vim.fn.writefile({ text }, path) ~= 0 then error("Could not write walkthrough file", 0) end
  return { ok = true, version = 3, total_steps = #steps }
end

local function load_walkthrough_file(args)
  if type(args.path) ~= "string" or trim(args.path) == "" then error("Load requires a file path", 0) end
  local path = vim.fn.fnamemodify(vim.fn.expand(args.path), ":p")
  local text = table.concat(vim.fn.readfile(path), "\n")
  -- Recognize the old marker header too, so a versioned JSON file cannot
  -- accidentally bypass portable validation through the legacy parser.
  local data = require("naia.walkthrough.response").json(text)
  local steps
  if type(data) == "table" and data.version ~= nil then
    if data.version == 3 then
      steps = require("naia.walkthrough.file").decode(vim.json.encode(data), args.root)
    elseif data.version == 2 then
      steps = data.steps
    else error("Unsupported walkthrough version", 0) end
  else
    steps = parse_walkthrough_response(text)
  end
  if not steps then error("Could not parse walkthrough file", 0) end
  local started, reason = start_walkthrough(steps, {
    confirm_start = args.confirm_start ~= false, pause_after_step = args.pause_after_step == true,
  })
  if reason == "awaiting_confirmation" then
    return { status = "awaiting_confirmation", total_steps = #steps }
  end
  if not started then
    if reason == "cancelled" then return { cancelled = true } end
    error("Could not start walkthrough", 0)
  end
  local ids = {}
  for _, entry in ipairs(playback_state.steps) do ids[#ids + 1] = entry.id end
  return { walkthrough_id = playback_state.walkthrough_id, revision = playback_state.revision, step_ids = ids }
end

local function export_walkthrough(file_arg, overwrite)
  local ok, result = pcall(export_walkthrough_file, { path = file_arg, overwrite = overwrite })
  vim.notify(ok and "AI walkthrough exported" or tostring(result), ok and vim.log.levels.INFO or vim.log.levels.ERROR)
end

local function import_walkthrough(file_arg)
  local ok, result = pcall(load_walkthrough_file, { path = file_arg })
  if not ok then vim.notify(tostring(result), vim.log.levels.ERROR) end
end

local function register_naia_walkthrough_tools()
  local ok, naia = pcall(require, "naia")
  if not ok then return end
  rich.register(enhanced, naia, {
    export = export_walkthrough_file,
    load = load_walkthrough_file,
    start = start_walkthrough,
    prepare = prepare_walkthrough_steps,
    changed = function(steps)
      last_walkthrough = vim.deepcopy(steps)
      if playback_state.browser_window and vim.api.nvim_win_is_valid(playback_state.browser_window) then
        render_walkthrough_browser_window()
      end
    end,
    jump = replay_walkthrough_step,
    pause = pause_walkthrough,
    resume = continue_walkthrough,
    stop = function()
      stop_walkthrough({ stop_voice = true, restore_display = true, restore_focus = true, close_created_window = true })
    end,
  })
end

local initialized = false
function M.setup()
  if initialized then return end
  set_walkthrough_highlight()
  register_naia_walkthrough_tools()

  require("naia.walkthrough.commands").setup({
    stop = function()
      stop_walkthrough({ stop_voice = true, restore_display = true, restore_focus = true,
        close_created_window = true, notify = true })
    end,
    repeat_tour = function(slow)
      if not last_walkthrough or #last_walkthrough == 0 then
        vim.notify("No AI walkthrough is available to repeat", vim.log.levels.WARN)
        return
      end
      start_walkthrough(last_walkthrough, { pause_after_step = slow })
    end,
    next = next_walkthrough_step, resume = continue_walkthrough, toggle = toggle_walkthrough_playback,
    pause = pause_walkthrough, previous = previous_walkthrough_step, parent = jump_to_parent_walkthrough_step,
    instruct = function(opts) require("naia.walkthrough.chat").instruct(playback_state, pause_walkthrough, opts) end,
    remove_enquiry = remove_current_walkthrough_enquiry, restart = reset_walkthrough,
    browser = browser.open, export = export_walkthrough, import = import_walkthrough,
  })

  vim.api.nvim_create_autocmd("User", {
    group = walkthrough_augroup,
    pattern = "AIVoicePlaybackStarted",
    callback = function(event)
      if not playback_state.active or not playback_state.waiting_for_voice then return end
      playback_state.voice_started = true
      playback_state.voice_request_id = event.data and event.data.request_id
    end,
  })

  vim.api.nvim_create_autocmd("User", {
    group = walkthrough_augroup,
    pattern = "AIVoicePlaybackStopped",
    callback = function(event)
      if not playback_state.active or not playback_state.waiting_for_voice or not playback_state.voice_started then return end
      local request_id = event.data and event.data.request_id
      if request_id and playback_state.voice_request_id and request_id ~= playback_state.voice_request_id then return end
      playback_state.waiting_for_voice = false
      playback_state.voice_started = false
      if playback_state.after_narration then playback_state.after_narration() end
    end,
  })

  vim.api.nvim_create_autocmd("ColorScheme", {
    group = walkthrough_augroup,
    callback = set_walkthrough_highlight,
  })
  vim.api.nvim_create_autocmd("VimLeavePre", {
    group = walkthrough_augroup,
    callback = function() stop_walkthrough({ stop_voice = false }) end,
  })
  initialized = true
end

function M.stop()
  return stop_walkthrough({ stop_voice = true, restore_display = true, restore_focus = true, close_created_window = true })
end

return M
