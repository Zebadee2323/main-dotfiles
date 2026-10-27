local M = {}
local style = require("naia.ui.style")
local companion_history, companion_index, companion_sequence = {}, 0, 0
local card_buffer, card_window
local card_history, card_index = {}, 0
local namespace = vim.api.nvim_create_namespace("naia_companion_links")

local function text(value, name)
  assert(type(value) == "string" and vim.trim(value) ~= "", name .. " must be non-empty text")
  assert(not value:find("%z"), name .. " must not contain NUL characters")
  return value
end

local function integer(value, default, name)
  value = value == nil and default or value
  assert(type(value) == "number" and value >= 1 and value % 1 == 0 and value < math.huge,
    name .. " must be a positive integer")
  return value
end

local function location(args)
  local path = text(args.path, "path")
  assert(not path:find("[\r\n]") and not path:match("^[%w+.-]+://"), "Only local file paths are supported")
  if path:sub(1, 2) == "~/" then
    path = vim.env.HOME .. path:sub(2)
  end
  path = vim.fs.normalize(vim.fn.fnamemodify(path, ":p"))
  assert(vim.fn.filereadable(path) == 1, "File is not readable: " .. path)
  return {
    path = path,
    line = integer(args.line, 1, "line"),
    column = integer(args.column, 1, "column"),
  }
end

local function editing_window()
  local current = vim.api.nvim_get_current_win()
  local windows = { current }
  vim.list_extend(windows, vim.api.nvim_tabpage_list_wins(0))
  for _, window in ipairs(windows) do
    local buffer = vim.api.nvim_win_get_buf(window)
    if vim.api.nvim_win_get_config(window).relative == "" and vim.bo[buffer].buftype == "" then
      return window
    end
  end
  error("No normal editing window is available in this tab")
end

local function navigation(target)
  -- Recheck at approval time, and use structured commands: paths are never Ex.
  target = location(target)
  local window = editing_window()
  vim.api.nvim_set_current_win(window)
  vim.api.nvim_win_call(window, function()
    vim.api.nvim_cmd({ cmd = "normal", bang = true, args = { "m'" } }, {})
    vim.api.nvim_cmd({ cmd = "edit", args = { target.path }, magic = { file = false, bar = false } }, {})
    local line = math.min(target.line, vim.api.nvim_buf_line_count(0))
    local content = vim.api.nvim_buf_get_lines(0, line - 1, line, false)[1] or ""
    local column = math.min(target.column - 1, #content)
    vim.api.nvim_win_set_cursor(window, { line, column })
    vim.api.nvim_cmd({ cmd = "normal", bang = true, args = { "zvzz" } }, {})
    target.line, target.column = line, column + 1
  end)
  return target
end

local function quickfix_snapshot()
  return vim.fn.getqflist({ id = 0, changedtick = 0 })
end

function M.prepare(kind, args)
  local reason = text(args.reason, "reason")
  assert(#reason <= 2000, "reason is too long")
  if kind == "navigation" then
    local target = location(args)
    return target, "Jump to a source location", { target.path, "Line " .. target.line .. ", column " .. target.column }, reason
  end
  if kind == "quickfix" then
    assert(type(args.items) == "table" and vim.islist(args.items) and #args.items <= 500,
      "items must be an array of at most 500 locations")
    local mode = args.mode or "new"
    assert(vim.tbl_contains({ "new", "append", "replace" }, mode), "Unknown quickfix mode")
    local title = text(args.title, "title")
    assert(#title <= 100 and not title:find("[\r\n]"), "title must be one line, at most 100 bytes")
    local items, preview = {}, { title }
    for _, item in ipairs(args.items) do
      local target = location(item)
      local message = text(item.text, "item text"):gsub("[\r\n]", " ")
      assert(#message <= 2000, "Quickfix item text exceeds 2000 bytes")
      local severity = item.type or "I"
      assert(vim.tbl_contains({ "E", "W", "I", "N" }, severity), "Invalid quickfix item type")
      items[#items + 1] = {
        filename = target.path, lnum = target.line, col = target.column, text = message, type = severity,
      }
      if #preview < 6 then
        preview[#preview + 1] = target.path .. ":" .. target.line .. " " .. message
      end
    end
    local snapshot = quickfix_snapshot()
    if mode ~= "new" then
      assert(snapshot.id ~= 0, "There is no existing quickfix list to " .. mode)
    end
    return { items = items, title = title, mode = mode, open = args.open == true, snapshot = snapshot },
      string.format("Quickfix: %s %d entries", mode, #items), preview, reason
  end
  assert(kind == "companion" or kind == "card", "Unknown UI capability")
  local content = text(args.content, "content")
  assert(#content <= 100000, "Companion content exceeds 100KB")
  local lines = vim.split(content, "\n", { plain = true })
  assert(#lines <= 1000, "Companion content exceeds 1000 lines")
  local title = text(args.title, "title")
  assert(#title <= 100 and not title:find("[\r\n]"), "title must be one line, at most 100 bytes")
  local links = {}
  assert(args.links == nil or (type(args.links) == "table" and vim.islist(args.links)), "links must be an array")
  assert(not args.links or #args.links <= 100, "Too many companion links")
  for _, link in ipairs(args.links or {}) do
    local target = location(link)
    local label = text(link.label, "link label")
    assert(not label:find("[\r\n]"), "link labels must be one line")
    local row = link.display_line and integer(link.display_line, nil, "display_line")
    if row then
      assert(row <= #lines and lines[row]:find(label, 1, true), "Link label must appear on its display_line")
    else
      if not vim.tbl_contains(lines, "## Sources") then
        lines[#lines + 1] = ""
        lines[#lines + 1] = "## Sources"
      end
      lines[#lines + 1] = string.format("- [%s](%s#L%d)", label, target.path, target.line)
      row = #lines
    end
    local start = assert(lines[row]:find(label, 1, true)) - 1
    links[#links + 1] = { target = target, row = row, column = start, finish = start + #label }
  end
  if kind == "card" then
    assert(#content <= 1200 and #lines <= 8, "Pinned cards must be at most 1200 bytes and 8 lines")
    local width, rows = math.min(46, vim.o.columns - 4), 0
    assert(width >= 16, "The editor is too narrow for a pinned card")
    for _, line in ipairs(lines) do rows = rows + math.max(1, math.ceil(vim.fn.strdisplaywidth(line) / width)) end
    assert(rows <= 8, "Pinned card text must fit within eight wrapped rows")
  end
  local preview = { title, string.format("%d lines, %d source links; opens without taking focus", #lines, #links) }
  for index = 1, math.min(4, #lines) do preview[#preview + 1] = lines[index] end
  return { lines = lines, title = title, links = links },
    kind == "card" and "Show a small pinned card (retains eight recent cards)" or "Create a new companion buffer", preview, reason
end

local function bind_links(buffer, links, request_navigation)
  for _, link in ipairs(links) do
    vim.api.nvim_buf_set_extmark(buffer, namespace, link.row - 1, link.column, {
      end_col = link.finish, hl_group = "Underlined",
    })
  end
  vim.keymap.set("n", "<CR>", function()
    local cursor = vim.api.nvim_win_get_cursor(0)
    local candidates, selected = {}, nil
    for _, link in ipairs(links) do
      if link.row == cursor[1] then
        candidates[#candidates + 1] = link
        if cursor[2] >= link.column and cursor[2] < link.finish then selected = link end
      end
    end
    selected = selected or (#candidates == 1 and candidates[1])
    if selected then
      local target = vim.deepcopy(selected.target)
      target.reason = "Open the source link selected in Naia's companion/card"
      request_navigation(target)
    else
      vim.notify("Place the cursor on a source link", vim.log.levels.INFO)
    end
  end, { buffer = buffer, desc = "Request navigation to source link" })
  for key, direction in pairs({ ["]l"] = 1, ["[l"] = -1 }) do
    vim.keymap.set("n", key, function()
      local cursor = vim.api.nvim_win_get_cursor(0)
      local ordered = vim.deepcopy(links)
      table.sort(ordered, function(a, b)
        return a.row < b.row or (a.row == b.row and a.column < b.column)
      end)
      if direction < 0 then ordered = vim.fn.reverse(ordered) end
      local chosen = ordered[1]
      for _, link in ipairs(ordered) do
        local after = link.row > cursor[1] or (link.row == cursor[1] and link.column > cursor[2])
        local before = link.row < cursor[1] or (link.row == cursor[1] and link.column < cursor[2])
        if (direction > 0 and after) or (direction < 0 and before) then chosen = link; break end
      end
      if chosen then vim.api.nvim_win_set_cursor(0, { chosen.row, chosen.column }) end
    end, { buffer = buffer, desc = "Move between source links" })
  end
end

local function prune_companions()
  local selected = companion_history[companion_index]
  local valid = {}
  for _, buffer in ipairs(companion_history) do
    if vim.api.nvim_buf_is_valid(buffer) and vim.api.nvim_buf_is_loaded(buffer) and vim.bo[buffer].buflisted then
      valid[#valid + 1] = buffer
    end
  end
  companion_history = valid
  companion_index = math.min(companion_index, #valid)
  for index, buffer in ipairs(valid) do
    if buffer == selected then companion_index = index end
  end
end

-- Displaying a companion only needs a split anchor, not a source-editing
-- buffer. Prefer an editing window, but allow terminal/nofile-only tabs too.
-- Keep editing_window() strict for source navigation.
local function companion_anchor()
  local windows = { vim.api.nvim_get_current_win() }
  vim.list_extend(windows, vim.api.nvim_tabpage_list_wins(0))
  local fallback
  for _, window in ipairs(windows) do
    if vim.api.nvim_win_get_config(window).relative == "" then
      if vim.bo[vim.api.nvim_win_get_buf(window)].buftype == "" then return window end
      fallback = fallback or window
    end
  end
  return assert(fallback, "No split window is available in this tab")
end

function M.open_companion(focus)
  prune_companions()
  local buffer = companion_history[companion_index]
  assert(buffer, "No companion content yet")
  local window
  for _, candidate in ipairs(vim.api.nvim_tabpage_list_wins(0)) do
    if vim.b[vim.api.nvim_win_get_buf(candidate)].naia_companion then window = candidate; break end
  end
  if window then
    -- Companion buffers are read-only, but the split is still an ordinary
    -- editing window: Telescope/:buffer must be able to replace its contents.
    -- Clear locks left on existing views by older versions as well.
    vim.wo[window].winfixbuf = false
    vim.api.nvim_win_set_buf(window, buffer)
    if focus then vim.api.nvim_set_current_win(window) end
  else
    local source = companion_anchor()
    local config = { win = source, split = vim.o.columns >= 100 and "right" or "below" }
    if config.split == "right" then
      config.width = math.min(60, math.floor(vim.api.nvim_win_get_width(source) * 0.4))
    else
      config.height = math.min(12, math.max(4, math.floor(vim.api.nvim_win_get_height(source) * 0.35)))
    end
    window = vim.api.nvim_open_win(buffer, focus == true, config)
    vim.wo[window].wrap = true
    vim.wo[window].linebreak = true
    vim.wo[window].number = false
    vim.wo[window].relativenumber = false
    vim.wo[window].winfixbuf = false
  end
  vim.wo[window].winbar = string.format("N.A.I.A · %s [%d/%d]",
    vim.b[buffer].naia_companion_title:gsub("%%", "%%%%"), companion_index, #companion_history)
  return window
end

function M.cycle_companion(direction)
  prune_companions()
  assert(#companion_history > 0, "No companion history")
  for index, buffer in ipairs(companion_history) do
    if buffer == vim.api.nvim_get_current_buf() then companion_index = index end
  end
  companion_index = (companion_index - 1 + direction) % #companion_history + 1
  return M.open_companion(true)
end

local function show_companion(data, request_navigation)
  companion_sequence = companion_sequence + 1
  local buffer = vim.api.nvim_create_buf(true, true)
  local slug = data.title:gsub("[^%w_-]", "_"):sub(1, 48)
  vim.api.nvim_buf_set_name(buffer, string.format("naia://companion/%04d-%s", companion_sequence, slug))
  vim.bo[buffer].bufhidden = "hide"
  vim.bo[buffer].swapfile = false
  vim.bo[buffer].filetype = "markdown"
  vim.b[buffer].naia_companion = true
  vim.b[buffer].naia_companion_title = data.title
  vim.api.nvim_buf_set_lines(buffer, 0, -1, false, data.lines)
  vim.bo[buffer].modified = false
  vim.bo[buffer].modifiable = false
  bind_links(buffer, data.links, request_navigation)
  vim.keymap.set("n", "q", function() vim.api.nvim_win_close(0, false) end,
    { buffer = buffer, desc = "Close companion view (retain buffer)" })
  for key, direction in pairs({ ["[h"] = -1, ["]h"] = 1 }) do
    vim.keymap.set("n", key, function() M.cycle_companion(direction) end,
      { buffer = buffer, desc = "Cycle companion history" })
  end
  companion_history[#companion_history + 1] = buffer
  companion_index = #companion_history
  local window = M.open_companion(false)
  vim.api.nvim_win_set_cursor(window, { 1, 0 })
  return { buffer = buffer, window = window, title = data.title, links = #data.links }
end

function M.close_card()
  if card_window and vim.api.nvim_win_is_valid(card_window) then vim.api.nvim_win_close(card_window, true) end
  if card_buffer and vim.api.nvim_buf_is_valid(card_buffer) then vim.api.nvim_buf_delete(card_buffer, { force = true }) end
  card_buffer, card_window = nil, nil
end

local function render_card(entry)
  local data = entry.data
  local width, rows = math.min(46, vim.o.columns - 4), 0
  assert(width >= 16, "The editor is too narrow for a pinned card")
  for _, line in ipairs(data.lines) do rows = rows + math.max(1, math.ceil(vim.fn.strdisplaywidth(line) / width)) end
  assert(rows <= 8 and rows <= vim.o.lines - vim.o.cmdheight - 5, "Pinned card does not fit in this editor")
  M.close_card()
  card_buffer = vim.api.nvim_create_buf(false, true)
  vim.bo[card_buffer].bufhidden = "wipe"
  vim.bo[card_buffer].swapfile = false
  vim.bo[card_buffer].filetype = "markdown"
  vim.api.nvim_buf_set_lines(card_buffer, 0, -1, false, data.lines)
  vim.bo[card_buffer].modified = false
  vim.bo[card_buffer].modifiable = false
  card_window = vim.api.nvim_open_win(card_buffer, false, {
    relative = "editor", anchor = "NE", row = 1, col = vim.o.columns - 2,
    width = width, height = math.max(1, rows), border = "rounded",
    title = string.format(" N.A.I.A · %s [%d/%d] ", data.title, card_index, #card_history), title_pos = "center",
    style = "minimal", focusable = false, mouse = false, zindex = 40,
  })
  vim.wo[card_window].wrap = true
  vim.wo[card_window].linebreak = false
  style.apply(card_window, "card")
  bind_links(card_buffer, data.links, entry.navigate)
  vim.keymap.set("n", "q", M.close_card, { buffer = card_buffer, desc = "Dismiss pinned Naia card" })
  for key, direction in pairs({ ["[h"] = -1, ["]h"] = 1 }) do
    vim.keymap.set("n", key, function() M.cycle_card(direction) end,
      { buffer = card_buffer, desc = "Cycle pinned card history" })
  end
  return { buffer = card_buffer, window = card_window, title = data.title }
end

function M.focus_card()
  if not card_window or not vim.api.nvim_win_is_valid(card_window) then
    assert(card_history[card_index], "No pinned card history")
    render_card(card_history[card_index])
  end
  vim.api.nvim_set_current_win(card_window)
end

function M.toggle_card()
  if card_window and vim.api.nvim_win_is_valid(card_window) then
    M.close_card()
  else
    M.focus_card()
  end
end

function M.cycle_card(direction)
  assert(#card_history > 0, "No pinned card history")
  local focused = card_window and vim.api.nvim_get_current_win() == card_window
  card_index = (card_index - 1 + direction) % #card_history + 1
  local result = render_card(card_history[card_index])
  if focused then vim.api.nvim_set_current_win(card_window) end
  return result
end

local function show_card(data, request_navigation)
  local entry = { data = vim.deepcopy(data), navigate = request_navigation }
  card_history[#card_history + 1] = entry
  if #card_history > 8 then table.remove(card_history, 1) end
  card_index = #card_history
  local result = render_card(entry)
  style.flash(result.window)
  return result
end

function M.apply(kind, data, request_navigation)
  if kind == "navigation" then return navigation(data) end
  if kind == "companion" then return show_companion(data, request_navigation) end
  if kind == "card" then return show_card(data, request_navigation) end
  local before = quickfix_snapshot()
  if data.mode ~= "new" then
    assert(before.id == data.snapshot.id and before.changedtick == data.snapshot.changedtick,
      "Quickfix changed while approval was pending; request a fresh update")
  end
  local what = { title = data.title, items = data.items }
  if data.mode ~= "new" then what.id = data.snapshot.id end
  local action = ({ new = " ", append = "a", replace = "r" })[data.mode]
  assert(vim.fn.setqflist({}, action, what) == 0, "Could not update quickfix")
  if data.open then
    local source = vim.api.nvim_get_current_win()
    vim.api.nvim_cmd({ cmd = "copen" }, {})
    if vim.api.nvim_win_is_valid(source) then vim.api.nvim_set_current_win(source) end
  end
  return { id = vim.fn.getqflist({ id = 0 }).id, count = #data.items, mode = data.mode }
end

return M
