local tools = {}
vim.fn.confirm = function() error("MCP confirmation must not block on vim.fn.confirm") end
vim.cmd("filetype plugin off") -- This isolated runtime intentionally has no TS parsers.
package.loaded.naia = {
  register_tool = function(name, spec) tools[name] = spec; return true end,
  deregister_tool = function(name) tools[name] = nil; return true end,
}
vim.g.ai_voice_enabled = false
vim.g.ai_walkthrough_post_voice_delay_ms = 1
vim.g.ai_walkthrough_voice_disabled_extra_delay_ms = 1
require("naia.walkthrough").setup()
vim.wait(20, function() return false end)

-- Validate the wire representation, not just the Lua table: MCP clients reject
-- an empty properties array even though an empty properties object is valid.
for name, tool in pairs(tools) do
  assert(type(tool.args) == "table", name .. " must use upstream argument definitions")
  assert(tool.input_schema == nil, name .. " must not depend on full-schema passthrough")
end

local function rawcall(name, args)
  return vim.json.decode(assert(tools["walkthrough_" .. name], name).callback(args or {}))
end
local function call(name, args)
  -- Playback tests opt out explicitly; rawcall exercises the real MCP default.
  if name == "start" and args.confirm_start == nil then
    args = vim.tbl_extend("force", args, { confirm_start = false })
  end
  return rawcall(name, args)
end
local function prompt_window()
  for _, win in ipairs(vim.api.nvim_list_wins()) do
    if vim.b[vim.api.nvim_win_get_buf(win)].naia_walkthrough_confirmation then return win end
  end
end
local function respond(key)
  local win = assert(prompt_window(), "Expected a non-blocking confirmation prompt")
  for _, map in ipairs(vim.api.nvim_buf_get_keymap(vim.api.nvim_win_get_buf(win), "n")) do
    if map.lhs == key then map.callback(); return end
  end
  error("Missing confirmation key: " .. key)
end
local path = vim.fn.tempname() .. ".txt"
vim.fn.writefile({ "local one = 1", "local two = 2", "return one + two" }, path)
local function step(description, extra)
  return vim.tbl_extend("force", { path = path, line_start = 1, line_end = 1, description = description }, extra or {})
end
local function context() return call("current_context", { include_tree = true, include_buffer = true, include_editor = true }) end
local function edit(operation, target, payload)
  local ctx = context()
  return call("edit", vim.tbl_extend("force", { operation = operation, step_id = target,
    walkthrough_id = ctx.walkthrough_id, expected_revision = ctx.revision }, payload or {}))
end
local function control(action, target)
  local ctx = context()
  return call("control", { action = action, step_id = target, walkthrough_id = ctx.walkthrough_id,
    expected_revision = ctx.revision, expected_current_step_id = ctx.current_step.id,
    expected_playback_revision = ctx.playback_revision })
end
local function rejects(fn, text)
  local ok, err = pcall(fn)
  assert(not ok and tostring(err):find(text, 1, true), tostring(err))
end

local pending = rawcall("start", { steps = { step("Root"), step("Next") }, pause_after_step = true })
assert(pending.status == "awaiting_confirmation" and pending.total_steps == 2 and not pending.walkthrough_id)
assert(prompt_window(), "MCP must return while confirmation is still open")
assert(tools.walkthrough_start.args.confirm_start.default == true)
assert(not tools.ai_walkthrough_start, "walkthrough tools must not retain the ai_ prefix")
respond("y")
assert(not prompt_window())
assert(context().total_steps == 2 and context().revision == 1)
local before_cancel = context()
assert(call("start", { steps = { step("Cancelled") }, confirm_start = true }).status == "awaiting_confirmation")
assert(context().walkthrough_id == before_cancel.walkthrough_id, "Pending requests must not replace the active tour")
respond("n")
assert(context().walkthrough_id == before_cancel.walkthrough_id and context().current_step.id == before_cancel.current_step.id,
  "Cancelling must preserve the existing tour")
assert(rawcall("start", { steps = { step("Dismissed") } }).status == "awaiting_confirmation")
vim.api.nvim_win_close(assert(prompt_window()), true)
assert(not prompt_window())
assert(context().walkthrough_id == before_cancel.walkthrough_id, "Dismissing must preserve the existing tour")
rawcall("start", { steps = { step("Superseded") } })
local superseded_prompt = assert(prompt_window())
rawcall("start", { steps = { step("Replacement request") } })
assert(not vim.api.nvim_win_is_valid(superseded_prompt) and prompt_window(), "New requests replace pending prompts")
local started = call("start", { steps = { step("Root"), step("Next") }, pause_after_step = true, confirm_start = false })
assert(not prompt_window(), "Explicit auto-start must skip confirmation")
rawcall("start", { steps = { step("Never start after stop") } })
control("stop")
assert(not prompt_window(), "Stopping must dismiss pending confirmation")
started = call("start", { steps = { step("Root"), step("Next") }, pause_after_step = true })
local root, next_id = unpack(started.step_ids)
assert(context().playback.paused)
local function header(win)
  return vim.api.nvim_eval_statusline(vim.wo[win].winbar, { winid = win, use_winbar = true, maxwidth = 1000 }).str
end
assert(header(0):find(vim.fn.fnamemodify(path, ":t"), 1, true), "Plain steps must prominently identify the file")
assert(header(0):find("L1", 1, true), "File headers must identify the focused lines")
assert(context().buffer.text:find("local two", 1, true))
local first = edit("append_children", root, { steps = { step("First branch") } }).step_ids[1]
local second = edit("append_children", root, { steps = { step("Second branch") } }).step_ids[1]
assert(context().total_steps == 4)
assert(context().current_step.id == root and context().playback.paused)
assert(context().upcoming[1].id == first and context().upcoming[2].id == second)
control("jump", first)
assert(context().ancestors[1].id == root and context().siblings[1].id == second)
edit("update_step", second, { step = step("Updated") })
control("jump", second)
assert(context().current_step.description == "Updated")
rejects(function() edit("remove_branch", second) end, "active step")
control("jump", root)
edit("remove_branch", first)
assert(context().total_steps == 3)
edit("replace_children", root, { steps = { step("Replacement") } })
assert(context().total_steps == 3 and context().upcoming[1].description == "Replacement")
local stale = context()
edit("append_children", root, { steps = { step("Another") } })
rejects(function() call("edit", { operation = "remove_branch", step_id = next_id,
  walkthrough_id = stale.walkthrough_id, expected_revision = stale.revision }) end, "Stale revision")
rejects(function() call("insert_steps", { steps = { step("Unsafe") }, parent_step_id = root }) end, "Stale walkthrough_id")
local before_invalid = context()
rejects(function() edit("append_children", root, { steps = { step("Valid"), step("Bad", { path = "/not/a/file" }) } }) end, "not found")
assert(context().revision == before_invalid.revision and context().total_steps == before_invalid.total_steps)

-- Live buffers and extmarks, including unsaved source edits.
local anchored = call("start", { pause_after_step = true, steps = {
  step("Anchored", { anchor = { snippet = "local two = 2" } }),
} })
local buf = vim.fn.bufnr(path)
vim.api.nvim_buf_set_lines(buf, 0, 0, false, { "-- inserted" })
assert(context().buffer.modified)
assert(context().current_step.line_start == 3)
control("jump", anchored.step_ids[1])
assert(context().current_step.line_start == 3)
vim.api.nvim_buf_set_lines(buf, 2, 3, false, {})
assert(context().location_error:find("deleted", 1, true))
vim.api.nvim_buf_set_lines(buf, 0, -1, false, { "same", "same", "tail" })
rejects(function() call("start", { steps = { step("Ambiguous", { anchor = { snippet = "same" } }) } }) end, "Ambiguous")
rejects(function() call("start", { steps = { step("Stale", { anchor = { snippet = "missing" } }) } }) end, "Stale")
call("start", { pause_after_step = true, steps = { step("Context anchor", {
  anchor = { snippet = "same", after = "tail" },
}) } })
assert(context().current_step.line_start == 2)

-- Beat playback advances highlights with narration, then pauses at the step boundary.
call("start", { pause_after_step = true, steps = { step("Overview", { beats = {
  { description = "First beat", line_start = 1, line_end = 1 },
  { description = "Second beat", line_start = 2, line_end = 2 },
} }), step("Next") } })
assert(vim.wait(200, function() return context().playback.paused end, 5))
assert(context().playback.beat_index == 2 and context().current_index == 1, vim.inspect(context()))
assert(header(0):find("L2", 1, true), "Narration beats must update the header's focused line range")
control("jump", context().upcoming[1].id)
assert(context().playback.beat_index == 1)

-- Before is a temporary left pane; current reuses the original right window.
control("stop")
local original = vim.api.nvim_get_current_win()
local saved_options = { wrap = true, foldmethod = "manual", foldcolumn = "1", foldlevel = 3,
  foldenable = true, scrollbind = false, cursorbind = false, winbar = "Original window" }
for name, value in pairs(saved_options) do vim.wo[original][name] = value end
local original_text = vim.api.nvim_buf_get_lines(buf, 0, -1, false)
local function normal_windows()
  local windows = {}
  for _, win in ipairs(vim.api.nvim_list_wins()) do
    if vim.api.nvim_win_get_config(win).relative == "" then windows[#windows + 1] = win end
  end
  return windows
end
local baseline_windows = #normal_windows()
local comparison = { left = { path = path, line_start = 1, line_end = 1, text = "before", label = "Before" },
  right = { path = path, line_start = 1, line_end = 1, text = "after", label = "After" }, diff = true }
vim.o.hidden = false
call("start", { pause_after_step = true, steps = { step("Compare", { comparison = comparison }), step("Plain") } })
local diff_windows = 0
for _, win in ipairs(vim.api.nvim_list_wins()) do if vim.wo[win].diff then diff_windows = diff_windows + 1 end end
assert(diff_windows == 2 and #normal_windows() == baseline_windows + 1,
  "Expected two code panes with no narration split")
assert(vim.wo[original].diff)
assert(vim.api.nvim_buf_get_lines(vim.api.nvim_win_get_buf(original), 0, 1, false)[1] == "after")
local left_window
for _, win in ipairs(normal_windows()) do
  if win ~= original then
    if vim.wo[win].diff then left_window = win end
  end
end
assert(vim.api.nvim_buf_get_lines(vim.api.nvim_win_get_buf(left_window), 0, 1, false)[1] == "before")
for _, win in ipairs({ original, left_window }) do
  assert(header(win):find(vim.fn.fnamemodify(path, ":t"), 1, true), "Labels must not replace comparison filenames")
  assert(header(win):find("Snapshot", 1, true), "Embedded snapshots must be clearly identified")
end
assert(header(original):find("Current | After", 1, true), "Custom labels must preserve the pane role")
assert(header(left_window):find("Before", 1, true))
assert(vim.api.nvim_win_get_position(left_window)[2] < vim.api.nvim_win_get_position(original)[2])
assert(not vim.wo[left_window].foldenable and not vim.wo[original].foldenable,
  "Comparisons must not independently collapse diff folds")
assert(context().buffer.text == "after", "Buffer context should describe the visible current pane")
assert(vim.api.nvim_win_get_height(original) == vim.api.nvim_win_get_height(left_window), "Diff panes must have equal heights")
local narration_window
for _, win in ipairs(vim.api.nvim_list_wins()) do
  local config = vim.api.nvim_win_get_config(win)
  if config.relative == "editor" then narration_window = win end
end
assert(narration_window, "Comparison narration must remain a floating box, not a split")
control("jump", context().upcoming[1].id)
assert(not vim.api.nvim_win_is_valid(narration_window), "The previous narration float must be cleaned up")
for _, win in ipairs(vim.api.nvim_list_wins()) do assert(not vim.wo[win].diff) end
assert(#normal_windows() == baseline_windows)
for name, value in pairs(saved_options) do
  if name ~= "winbar" then assert(vim.wo[original][name] == value, "Failed to restore " .. name) end
end
assert(header(original):find(vim.fn.fnamemodify(path, ":t"), 1, true) and not header(original):find("Snapshot", 1, true),
  "Leaving a comparison must replace its header with the plain file header")
assert(vim.deep_equal(vim.api.nvim_buf_get_lines(buf, 0, -1, false), original_text))
assert(vim.bo[buf].modified, "Unsaved source changes must survive comparison cleanup")
-- Plain/snapshot steps need the same collision avoidance as comparisons. A
-- short declaration at the top used to be covered by the top-right float.
call("start", { pause_after_step = true, steps = { step("Explain this declaration without covering it.", { line_end = 3 }) } })
vim.cmd("redraw")
local plain_float
for _, win in ipairs(vim.api.nvim_list_wins()) do
  if vim.api.nvim_win_get_config(win).relative ~= "" then plain_float = win end
end
assert(plain_float, "Plain narration must remain a float")
local plain_top = vim.api.nvim_win_get_position(plain_float)[1]
local plain_bottom = plain_top + vim.api.nvim_win_get_height(plain_float) + 1
for line = 1, 3 do
  local row = vim.fn.screenpos(original, line, 1).row - 1
  assert(row < plain_top or row > plain_bottom, "Plain narration must avoid the whole explained range")
end
control("stop")
assert(vim.wo[original].winbar == saved_options.winbar, "Stopping a plain step must restore the original header: "
  .. vim.inspect(vim.wo[original].winbar))

-- File identity comes from source paths, even for scratch snapshots and labels
-- containing statusline syntax. Changing files must not leave a stale title.
local special_path = vim.fn.tempname() .. "%file.txt"
vim.fn.writefile({ "first", "second" }, special_path)
local saved_hidden = vim.o.hidden
vim.o.hidden = true
local header_tour = call("start", { pause_after_step = true, steps = {
  step("Named snapshot", { path = special_path, text = "first\nsecond", line_end = 2, label = "100% ready %{1+1}" }),
  step("Committed source", { path = "README.md", revision = "HEAD" }),
} })
local snapshot_win
for _, win in ipairs(normal_windows()) do
  if header(win):find(vim.fn.fnamemodify(special_path, ":t"), 1, true) then snapshot_win = win end
end
assert(snapshot_win, "Snapshot headers must use the source filename: " .. vim.inspect(vim.tbl_map(header, normal_windows())))
assert(header(snapshot_win):find("100% ready %{1+1}", 1, true), "Header text must not evaluate statusline expressions")
assert(header(snapshot_win):find("L1-2", 1, true))
control("jump", header_tour.step_ids[2])
assert(header(snapshot_win):find("README.md", 1, true) and header(snapshot_win):find("@HEAD", 1, true))
assert(not header(snapshot_win):find("%file.txt", 1, true), "File changes must replace the previous header")
control("stop")
assert(vim.wo[original].winbar == saved_options.winbar, "Snapshot cleanup must restore the user's header")
vim.o.hidden = saved_hidden
vim.fn.delete(special_path)

-- Plain steps must use native zz centering, not a topline clamped to the last
-- full screen of buffer lines. Include a winbar and wrapped context lines.
local centering_lines = {}
for i = 1, 200 do centering_lines[i] = "line " .. i end
centering_lines[105] = string.rep("wrapped context ", 12)
saved_hidden = vim.o.hidden
vim.o.hidden = true
local centering_tour = call("start", { pause_after_step = true, steps = {
  step("Near EOF", { text = table.concat(centering_lines, "\n"), line_start = 190, line_end = 194 }),
  step("Wrapped context", { text = table.concat(centering_lines, "\n"), line_start = 110, line_end = 112 }),
  step("Tall range", { text = table.concat(centering_lines, "\n"), line_start = 80, line_end = 150 }),
} })
local function assert_native_centering(midpoint)
  vim.cmd("redraw")
  local info = vim.fn.getwininfo(original)[1]
  local row = vim.fn.screenpos(original, midpoint, 1).row
  local before = vim.api.nvim_win_call(original, function() return vim.fn.winsaveview() end)
  vim.api.nvim_win_call(original, function() vim.cmd("normal! zz") end)
  vim.cmd("redraw")
  local after = vim.api.nvim_win_call(original, function() return vim.fn.winsaveview() end)
  assert(before.topline == after.topline and before.skipcol == after.skipcol and before.topfill == after.topfill,
    "Walkthrough focus must already match native zz: " .. vim.inspect({ before = before, after = after }))
  assert(math.abs(row - (info.winrow + (info.winbar or 0) + math.floor(info.height / 2))) <= 1,
    "The focused midpoint should be vertically centered")
end
assert_native_centering(192)
control("jump", centering_tour.step_ids[2])
assert_native_centering(111)
control("jump", centering_tour.step_ids[3])
assert(vim.api.nvim_win_get_cursor(original)[1] == 80, "Tall ranges must focus their beginning")
assert(vim.api.nvim_win_call(original, function() return vim.fn.winsaveview().topline end) == 80,
  "Tall ranges must start at the top of the viewport")
control("stop")
vim.o.hidden = saved_hidden

-- Uneven user splits must not be equalized when comparison panes come/go.
-- Include a nested horizontal split to exercise both dimensions.
local saved_columns, saved_lines = vim.o.columns, vim.o.lines
local equalalways = vim.o.equalalways
vim.o.equalalways = true
vim.o.columns, vim.o.lines = 180, 50
vim.cmd("redraw")
vim.api.nvim_set_current_win(original)
vim.cmd("rightbelow vnew")
local sidebar = vim.api.nvim_get_current_win()
vim.api.nvim_win_set_width(sidebar, 27)
vim.cmd("belowright new")
local lower_sidebar = vim.api.nvim_get_current_win()
vim.api.nvim_win_set_height(lower_sidebar, 9)
vim.api.nvim_win_set_width(sidebar, 27)
vim.api.nvim_set_current_win(original)
local function dimensions(win)
  return { vim.api.nvim_win_get_width(win), vim.api.nvim_win_get_height(win), vim.api.nvim_win_get_position(win) }
end
local sidebar_size, lower_size, original_size = dimensions(sidebar), dimensions(lower_sidebar), dimensions(original)
assert(sidebar_size[1] == 27 and lower_size[2] == 9, "Regression requires uneven user splits")
local size_tour = call("start", { pause_after_step = true, steps = {
  step("First comparison", { comparison = comparison }),
  step("Second comparison", { comparison = comparison }), step("Plain again"),
  step("First snapshot", { text = "first snapshot" }),
  step("Second snapshot", { text = "second snapshot" }),
} })
local function assert_user_sizes(comparing)
  assert(vim.deep_equal(dimensions(sidebar), sidebar_size), "Comparison resized the user's sidebar: "
    .. vim.inspect({ before = sidebar_size, after = dimensions(sidebar) }))
  assert(vim.deep_equal(dimensions(lower_sidebar), lower_size), "Comparison resized the user's nested split")
  if not comparing then
    assert(vim.deep_equal(dimensions(original), original_size), "Comparison did not restore the source window size")
  end
  assert(vim.o.equalalways, "Walkthrough must restore the user's equalalways option")
end
assert_user_sizes(true)
control("jump", size_tour.step_ids[2])
assert_user_sizes(true)
control("jump", size_tour.step_ids[3])
assert_user_sizes(false)
vim.o.hidden = true
control("jump", size_tour.step_ids[4])
assert_user_sizes(false)
control("jump", size_tour.step_ids[5])
assert_user_sizes(false)
control("jump", size_tour.step_ids[1])
control("stop")
assert_user_sizes(false)
vim.o.equalalways = equalalways
vim.api.nvim_win_close(lower_sidebar, true)
vim.api.nvim_win_close(sidebar, true)
vim.o.columns, vim.o.lines = saved_columns, saved_lines
vim.api.nvim_set_current_win(original)
-- Stopping directly in a comparison restores the borrowed window before source layout restoration.
call("start", { pause_after_step = true, steps = { step("Compare again", { comparison = comparison }) } })
control("stop")
assert(vim.api.nvim_win_get_buf(original) == buf and #normal_windows() == baseline_windows)
for name, value in pairs(saved_options) do assert(vim.wo[original][name] == value, "Stop failed to restore " .. name) end
-- A one-sided inserted line uses diff filler, not a shifted narration layout.
local before_lines, after_lines = {}, {}
for i = 1, 30 do
  before_lines[#before_lines + 1] = "line " .. i
  if i == 3 then after_lines[#after_lines + 1] = "inserted line" end
  after_lines[#after_lines + 1] = i % 5 == 0 and ("changed " .. i) or ("line " .. i)
end
call("start", { pause_after_step = true, steps = { step("Aligned narration", { comparison = {
  left = { path = path, line_start = 22, line_end = 22, text = table.concat(before_lines, "\n") },
  right = { path = path, line_start = 23, line_end = 23, text = table.concat(after_lines, "\n") }, diff = true,
} }) } })
for _, win in ipairs(normal_windows()) do if win ~= original and vim.wo[win].diff then left_window = win end end
vim.cmd("redraw")
local left_row = vim.fn.screenpos(left_window, 22, 1).row
local right_row = vim.fn.screenpos(original, 23, 1).row
assert(left_row > 0 and left_row == right_row, "Diff rows must align while floating narration is visible")
control("stop")
-- A left-only deletion/filler and both explained ranges must be avoided by
-- the float's entire rectangle, without shifting either pane to make room.
before_lines, after_lines = {}, {}
for i = 1, 45 do
  before_lines[#before_lines + 1] = "value " .. i
  if i < 3 or i > 5 then after_lines[#after_lines + 1] = "value " .. i end
end
call("start", { pause_after_step = true, steps = { step("Position this narration below the changes, not over either side.", { comparison = {
  left = { path = path, line_start = 8, line_end = 8, text = table.concat(before_lines, "\n") },
  right = { path = path, line_start = 5, line_end = 5, text = table.concat(after_lines, "\n") }, diff = true,
} }) } })
narration_window = nil
for _, win in ipairs(vim.api.nvim_list_wins()) do
  if win ~= original and vim.wo[win].diff then left_window = win end
  if vim.api.nvim_win_get_config(win).relative == "editor" then narration_window = win end
end
assert(narration_window)
local float_top = vim.api.nvim_win_get_position(narration_window)[1]
local float_bottom = float_top + vim.api.nvim_win_get_height(narration_window) + 1
local function outside_float(win, line)
  local row = vim.fn.screenpos(win, line, 1).row - 1
  assert(row < float_top or row > float_bottom, "Narration border/text must avoid protected rows")
end
outside_float(left_window, 8)
outside_float(original, 5)
for line = 3, 5 do outside_float(left_window, line) end
local function view(win) return vim.api.nvim_win_call(win, function() return vim.fn.winsaveview() end) end
local left_view, right_view = view(left_window), view(original)
require("naia.walkthrough.layout").place(original, {
  { win = left_window, loc = { line_start = 8, line_end = 8 } },
  { win = original, loc = { line_start = 5, line_end = 5 } },
}, {}, 6, 0)
assert(vim.deep_equal(left_view, view(left_window)) and vim.deep_equal(right_view, view(original)),
  "Placement calculation must not scroll either diff pane")
control("stop")
-- Long unchanged sections plus insertions/deletions used to produce different
-- closed folds and screen offsets on the two sides (missed by the tiny fixture).
local original_diffopt = vim.o.diffopt
vim.opt.diffopt:append("linematch:40")
before_lines, after_lines = {}, {}
local correspondence = {}
for i = 1, 240 do
  local line = string.format("local value_%03d = %d", i, i)
  before_lines[#before_lines + 1] = line
  if i == 65 then
    for j = 1, 8 do after_lines[#after_lines + 1] = "-- inserted " .. j end
  end
  if i < 73 or i > 77 then
    after_lines[#after_lines + 1] = line
    correspondence[i] = #after_lines
  end
end
call("start", { pause_after_step = true, steps = { step("Folded diff regression", { comparison = {
  left = { path = path, line_start = 80, line_end = 82, text = table.concat(before_lines, "\n") },
  right = { path = path, line_start = correspondence[80], line_end = correspondence[82], text = table.concat(after_lines, "\n") },
  diff = true,
} }) } })
for _, win in ipairs(normal_windows()) do if win ~= original and vim.wo[win].diff then left_window = win end end
assert(not vim.wo[left_window].foldenable and not vim.wo[original].foldenable)
vim.cmd("redraw")
local checked_rows = 0
for line, current_line in pairs(correspondence) do
  local before_row = vim.fn.screenpos(left_window, line, 1).row
  local current_row = vim.fn.screenpos(original, current_line, 1).row
  if before_row > 0 and current_row > 0 then
    assert(before_row == current_row, "Corresponding diff lines must share a screen row: " .. line)
    checked_rows = checked_rows + 1
  end
end
assert(checked_rows >= 3, "Regression must verify multiple visible matching lines")
assert(vim.fn.screenpos(left_window, 80, 1).row == vim.fn.screenpos(original, correspondence[80], 1).row)
control("stop")
assert(vim.wo[original].foldenable, "Restore the user's original folding after comparison")
vim.o.diffopt = original_diffopt
call("start", { pause_after_step = true, steps = { step("Continue") } })
local old = context()
call("start", { pause_after_step = true, steps = { step("New tour") } })
rejects(function() call("control", { action = "stop", walkthrough_id = old.walkthrough_id,
  expected_revision = old.revision, expected_current_step_id = old.current_step.id }) end, "Stale walkthrough_id")

-- Labeled ranges, Git snapshots, and lossless rich export/import.
local rich_step = step("Rich", {
  highlights = { { path = path, line_start = 2, line_end = 2, label = "consumer" } },
  comparison = {
    left = { path = "README.md", line_start = 1, line_end = 1, revision = "HEAD", label = "Committed" },
    right = { path = path, line_start = 1, line_end = 1, label = "Current" }, diff = true,
  },
})
call("start", { pause_after_step = true, steps = { rich_step } })
local marks = vim.api.nvim_buf_get_extmarks(buf, vim.api.nvim_create_namespace("naia_walkthrough_ranges"), 0, -1, { details = true })
local consumer_found = false
for _, mark in ipairs(marks) do
  if mark[4].virt_text and mark[4].virt_text[1][1] == " consumer" then consumer_found = true end
end
assert(consumer_found)
local export = vim.fn.tempname() .. ".yaml"
rejects(function() call("export", { path = export }) end, "outside the root")
rich_step.path = "README.md"
rich_step.highlights[1].path = "README.md"
rich_step.comparison.right.path = "README.md"
call("start", { pause_after_step = true, steps = { rich_step } })
vim.cmd("AIWalkExport " .. vim.fn.fnameescape(export))
local exported = table.concat(vim.fn.readfile(export), "\n")
assert(exported:find('"comparison"', 1, true) and not exported:find('"_mark"', 1, true))
assert(not exported:find(vim.fn.getcwd(), 1, true) and not exported:find('resolved_path', 1, true))
assert(vim.json.decode(exported).version == 3)
assert(call("file_spec").schema.properties.version.enum[1] == 3)
rejects(function() call("export", { path = export }) end, "already exists")
assert(call("export", { path = export, overwrite = true }).ok)
local before_load = context().walkthrough_id
assert(call("load", { path = export }).status == "awaiting_confirmation")
respond("n")
assert(context().walkthrough_id == before_load)
vim.cmd("AIWalkImport " .. vim.fn.fnameescape(export))
respond("y")
assert(context().current_step.comparison.left.revision == "HEAD")
assert(context().current_step.highlights[1].label == "consumer")
assert(call("load", { path = export, pause_after_step = true, confirm_start = false }).walkthrough_id)
local valid_tour = context().walkthrough_id
local invalid = vim.json.decode(exported)
invalid.steps[1].comparison.left.path = "../private.txt"
vim.fn.writefile({ "# ai-walkthrough", vim.json.encode(invalid) }, export)
rejects(function() call("load", { path = export, confirm_start = false }) end, "cannot contain")
assert(context().walkthrough_id == valid_tour, "Invalid loads must preserve the current tour")
invalid.version = 99
vim.fn.writefile({ vim.json.encode(invalid) }, export)
rejects(function() call("load", { path = export }) end, "Unsupported walkthrough version")
-- Version-2 files with the old header remain readable.
local legacy = vim.json.decode(exported)
legacy.version = 2
vim.fn.writefile({ "# ai-walkthrough", vim.json.encode(legacy) }, export)
assert(call("load", { path = export, confirm_start = false, pause_after_step = true }).walkthrough_id)
vim.fn.delete(export)

-- Symbol resolution uses LSP and rejects ambiguous names rather than guessing.
local request = vim.lsp.buf_request_sync
vim.lsp.buf_request_sync = function()
  return { { result = { { name = "target", range = { start = { line = 1, character = 0 },
    ["end"] = { line = 2, character = 0 } } } } } }
end
call("start", { pause_after_step = true, steps = { step("Symbol", { anchor = { symbol = "target" } }) } })
assert(context().current_step.line_start == 2)
vim.lsp.buf_request_sync = request

-- Voice boundaries advance exactly one beat; unrelated/late stop events do not.
local spoken, voice_id = {}, 0
vim.api.nvim_create_user_command("AIVoiceStop", function() end, {})
vim.api.nvim_create_user_command("AIVoiceInterrupt", function(opts)
  spoken[#spoken + 1] = opts.args
  voice_id = voice_id + 1
  vim.api.nvim_exec_autocmds("User", { pattern = "AIVoicePlaybackStarted", data = { request_id = voice_id } })
end, { nargs = "+" })
vim.g.ai_voice_enabled = true
call("start", { pause_after_step = true, steps = { step("Voice", { beats = {
  { description = "Voice one" }, { description = "Voice two", line_start = 2, line_end = 2 },
} }) } })
assert(spoken[#spoken] == "Voice one" and context().playback.speaking)
vim.api.nvim_exec_autocmds("User", { pattern = "AIVoicePlaybackStopped", data = { request_id = 9999 } })
assert(context().playback.speaking and context().playback.beat_index == 1)
vim.api.nvim_exec_autocmds("User", { pattern = "AIVoicePlaybackStopped", data = { request_id = voice_id } })
assert(vim.wait(200, function() return spoken[#spoken] == "Voice two" end, 5))
vim.api.nvim_exec_autocmds("User", { pattern = "AIVoicePlaybackStopped", data = { request_id = voice_id } })
assert(context().playback.paused)
local position = context()
control("jump", position.current_step.id)
rejects(function() call("control", { action = "stop", walkthrough_id = position.walkthrough_id,
  expected_revision = position.revision, expected_current_step_id = position.current_step.id,
  expected_playback_revision = position.playback_revision }) end, "Stale playback revision")
control("stop")
vim.bo[buf].modified = false
vim.fn.delete(path)
print("walkthrough tests passed")
