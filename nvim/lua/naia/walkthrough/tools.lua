-- Rich walkthrough data, stable locations, and the MCP editing interface.
-- Playback stays in walkthrough/init.lua; this module never applies source edits.
local M = {}
local api = vim.api
local layout = require("naia.walkthrough.layout")
local anchor_ns = api.nvim_create_namespace("naia_walkthrough_anchors")
local highlight_ns = api.nvim_create_namespace("naia_walkthrough_ranges")
-- :diffthis changes several local options in addition to 'diff'. Save them
-- explicitly because the current/right pane is a borrowed source window.
local comparison_options = {
  "diff", "scrollbind", "cursorbind", "wrap", "foldmethod", "foldcolumn",
  "foldlevel", "foldenable", "foldminlines", "foldnestmax", "foldexpr", "foldtext", "winbar",
}

local function show_buffer(win, buf)
  if api.nvim_win_get_buf(win) ~= buf then
    -- Hide rather than abandon a modified buffer, even with 'hidden' disabled.
    api.nvim_win_call(win, function() vim.cmd("hide buffer " .. buf) end)
  end
end

local function fail(message)
  error(message, 0)
end

local function integer(n, name)
  if type(n) ~= "number" or n < 1 or n % 1 ~= 0 then
    fail(name .. " must be a positive integer")
  end
  return n
end

local function public(value)
  if type(value) ~= "table" then return value end
  local result = {}
  for k, v in pairs(value) do
    if type(k) ~= "string" or k:sub(1, 1) ~= "_" then
      if k ~= "resolved_path" then result[k] = public(v) end
    end
  end
  return result
end

local function lines_text(buf, first, last)
  return table.concat(api.nvim_buf_get_lines(buf, first - 1, last, false), "\n")
end

local function symbols(buf, name)
  local found, seen = {}, {}
  local function add(first, last)
    local key = first .. ":" .. last
    if not seen[key] then
      seen[key] = true
      found[#found + 1] = { first, last }
    end
  end
  -- DocumentSymbol ranges are already line-based; position encodings do not matter.
  local responses = vim.lsp.buf_request_sync(buf, "textDocument/documentSymbol", {
    textDocument = { uri = vim.uri_from_bufnr(buf) },
  }, 700) or {}
  local function visit(items)
    for _, item in ipairs(items or {}) do
      local range = item.range or (item.location and item.location.range)
      if item.name == name and range then
        add(range.start.line + 1, math.max(range.start.line + 1,
          range["end"].line + (range["end"].character > 0 and 1 or 0)))
      end
      visit(item.children)
    end
  end
  for _, response in pairs(responses) do visit(response.result) end
  if #found > 0 then return found end
  -- Tree-sitter fallback for declarations with a named `name` field.
  local ok, parser = pcall(vim.treesitter.get_parser, buf)
  if ok and parser then
    local parsed, trees = pcall(parser.parse, parser)
    if parsed then
      local function walk(node)
        local names = node:field("name")
        if names[1] and vim.treesitter.get_node_text(names[1], buf) == name then
          local first, _, last, col = node:range()
          add(first + 1, math.max(first + 1, last + (col > 0 and 1 or 0)))
        end
        for child in node:iter_children() do walk(child) end
      end
      for _, tree in ipairs(trees) do walk(tree:root()) end
    end
  end
  return found
end

function M.new(state, resolve_path)
  local self = { state = state, resolve_path = resolve_path, marks = {}, windows = {}, buffers = {}, winbars = {} }

  function self:save_file_header(win)
    if self.winbars[win] == nil then self.winbars[win] = vim.wo[win].winbar end
  end

  function self:show_file_header(win, loc, role)
    self:save_file_header(win)
    -- Use the source location, not the scratch buffer name, for snapshots.
    local path = vim.fn.fnamemodify(loc.resolved_path or loc.path, ":~:.")
    local function literal(text)
      return text:gsub("%c", " "):gsub("%%", "%%%%")
    end
    local details = {}
    if role then details[#details + 1] = role end
    if loc.label and loc.label ~= role then details[#details + 1] = loc.label end
    if loc.revision then details[#details + 1] = "@" .. loc.revision
    elseif loc.text then details[#details + 1] = "Snapshot" end
    local range = "L" .. loc.line_start
    if loc.line_end ~= loc.line_start then range = range .. "-" .. loc.line_end end
    -- Keep the filename first; truncate the path before losing its identity.
    vim.wo[win].winbar = " %#Title#" .. literal(vim.fn.fnamemodify(path, ":t")) .. "%*"
      .. (#details > 0 and ("  [" .. literal(table.concat(details, " | ")) .. "]") or "")
      .. "  " .. range .. "  %<" .. literal(path)
  end

  function self:cleanup_views()
    self.comparison_panes = nil
    for _, win in ipairs(self.windows) do
      if api.nvim_win_is_valid(win) then
        pcall(layout.without_equalizing, function() api.nvim_win_close(win, true) end)
      end
    end
    local borrowed = self.borrowed_window
    self.borrowed_window = nil
    if borrowed and api.nvim_win_is_valid(borrowed.win) then
      if api.nvim_buf_is_valid(borrowed.buf) then pcall(show_buffer, borrowed.win, borrowed.buf) end
      -- Restore 'diff' first, then undo any local-option changes it triggers.
      for _, name in ipairs(comparison_options) do
        pcall(api.nvim_set_option_value, name, borrowed.options[name], { win = borrowed.win })
      end
      pcall(api.nvim_win_call, borrowed.win, function() vim.fn.winrestview(borrowed.view) end)
    end
    for _, buf in ipairs(self.buffers) do
      if api.nvim_buf_is_valid(buf) then
        -- Wiping a snapshot while it is still displayed can CLOSE the source
        -- split, redistributing its space to the user's other panes. Detach it
        -- first, just as we restore the borrowed window for comparisons.
        for _, win in ipairs(vim.fn.win_findbuf(buf)) do
          local display = state.display_state
          local fallback = display and display.buffer_id
          if not (fallback and fallback ~= buf and api.nvim_buf_is_valid(fallback)) then
            fallback = api.nvim_create_buf(true, false)
          end
          show_buffer(win, fallback)
        end
        if api.nvim_buf_is_valid(buf) then pcall(api.nvim_buf_delete, buf, { force = true }) end
      end
    end
    -- Buffer switches can reset window-local options; restore titles last.
    for win, winbar in pairs(self.winbars) do
      if api.nvim_win_is_valid(win) then pcall(api.nvim_set_option_value, "winbar", winbar, { win = win }) end
    end
    self.winbars = {}
    self.windows, self.buffers = {}, {}
    for _, buf in ipairs(api.nvim_list_bufs()) do
      if api.nvim_buf_is_loaded(buf) then api.nvim_buf_clear_namespace(buf, highlight_ns, 0, -1) end
    end
  end

  function self:clear()
    self:cleanup_views()
    for buf in pairs(self.marks) do
      if api.nvim_buf_is_valid(buf) then api.nvim_buf_clear_namespace(buf, anchor_ns, 0, -1) end
    end
    self.marks = {}
  end

  function self:touch()
    state.revision = (state.revision or 0) + 1
  end

  function self:started()
    state.walkthrough_id = tostring((vim.uv or vim.loop).hrtime())
    state.revision = 1
    state.finished = false
    state.beat_index = 1
    state.playback_revision = 0
  end

  function self:location(raw, fallback)
    if type(raw) ~= "table" then fail("A code location must be an object") end
    local loc = public(raw)
    loc.path = loc.path or (fallback and fallback.path)
    loc.line_start = loc.line_start or (fallback and fallback.line_start)
    loc.line_end = loc.line_end or (fallback and fallback.line_end)
    if type(loc.path) ~= "string" or loc.path == "" then fail("Location requires path") end
    integer(loc.line_start, "line_start")
    integer(loc.line_end, "line_end")
    if loc.line_end < loc.line_start then fail("line_end precedes line_start") end
    loc.resolved_path = self.resolve_path(loc.path)
    if not loc.resolved_path and (loc.revision or loc.text) then
      -- A historical or proposed file need not exist in the working tree.
      loc.resolved_path = vim.fn.fnamemodify(loc.path, ":p")
    end
    if not loc.resolved_path then fail("Code file not found: " .. loc.path) end
    if loc.anchor then
      if type(loc.anchor) ~= "table" or
        (type(loc.anchor.snippet) ~= "string" and type(loc.anchor.symbol) ~= "string") then
        fail("anchor requires snippet or symbol")
      end
    end
    self:resolve(loc)
    return loc
  end

  function self:buffer(loc)
    if loc.revision or loc.text then
      if loc._buffer and api.nvim_buf_is_valid(loc._buffer) then return loc._buffer end
      local text = loc.text
      if not text then
        if type(loc.revision) ~= "string" or loc.revision == "" or loc.revision:find("[%c]") then
          fail("Invalid Git revision")
        end
        local root_result = vim.system({ "git", "-C", vim.fn.fnamemodify(loc.resolved_path, ":h"),
          "rev-parse", "--show-toplevel" }, { text = true }):wait()
        if root_result.code ~= 0 then fail("Comparison file is not in a Git repository") end
        local root = vim.trim(root_result.stdout)
        local rel = vim.fs.relpath(root, loc.resolved_path)
        local result = vim.system({ "git", "-C", root, "show", loc.revision .. ":" .. rel }, { text = true }):wait()
        if result.code ~= 0 then fail("Cannot read Git snapshot: " .. vim.trim(result.stderr or "")) end
        text = result.stdout:gsub("\n$", "")
      end
      if type(text) ~= "string" then fail("Snapshot text must be a string") end
      local buf = api.nvim_create_buf(false, true)
      api.nvim_buf_set_lines(buf, 0, -1, false, vim.split(text, "\n", { plain = true }))
      vim.bo[buf].buftype = "nofile"
      vim.bo[buf].bufhidden = "wipe"
      vim.bo[buf].swapfile = false
      vim.bo[buf].modifiable = false
      vim.bo[buf].filetype = vim.filetype.match({ filename = loc.path }) or ""
      loc._buffer = buf
      self.buffers[#self.buffers + 1] = buf
      return buf
    end
    local buf = vim.fn.bufadd(loc.resolved_path)
    vim.fn.bufload(buf)
    return buf
  end

  function self:resolve(loc)
    local buf = self:buffer(loc)
    local count = api.nvim_buf_line_count(buf)
    if loc._mark and loc._buffer == buf then
      local mark = api.nvim_buf_get_extmark_by_id(buf, anchor_ns, loc._mark, { details = true })
      if #mark > 0 then
        local details = mark[3]
        if details.invalid or (details.end_row or mark[1]) <= mark[1] then fail("Code anchor was deleted: " .. loc.path) end
        loc.line_start, loc.line_end = mark[1] + 1, details.end_row
        return buf
      end
    end
    local anchor = loc.anchor
    if anchor then
      local matches = {}
      if anchor.snippet then
        local snippet = vim.split(anchor.snippet, "\n", { plain = true })
        if anchor.snippet == "" then fail("Anchor snippet cannot be empty") end
        for first = 1, count - #snippet + 1 do
          if lines_text(buf, first, first + #snippet - 1) == anchor.snippet then
            local before = anchor.before
            local after = anchor.after
            local before_count = before and #vim.split(before, "\n", { plain = true }) or 0
            local after_count = after and #vim.split(after, "\n", { plain = true }) or 0
            if (not before or (first > before_count and lines_text(buf, first - before_count, first - 1) == before))
              and (not after or lines_text(buf, first + #snippet, first + #snippet + after_count - 1) == after) then
              matches[#matches + 1] = { first, first + #snippet - 1 }
            end
          end
        end
      else
        matches = symbols(buf, anchor.symbol)
      end
      if #matches ~= 1 then fail((#matches == 0 and "Stale" or "Ambiguous") .. " code anchor: " .. loc.path) end
      loc.line_start, loc.line_end = matches[1][1], matches[1][2]
    end
    if loc.line_start > count or loc.line_end > count then fail("Code range is outside buffer: " .. loc.path) end
    loc._buffer = buf
    loc._mark = api.nvim_buf_set_extmark(buf, anchor_ns, loc.line_start - 1, 0, {
      end_row = loc.line_end, end_col = 0, right_gravity = true, end_right_gravity = false,
      invalidate = true, strict = false,
    })
    self.marks[buf] = true
    return buf
  end

  function self:normalize(raw_steps)
    if type(raw_steps) ~= "table" or not vim.islist(raw_steps) or #raw_steps == 0 then fail("steps must be a non-empty array") end
    local steps = {}
    local function extras(target, raw)
      if raw.highlights then
        target.highlights = {}
        for _, range in ipairs(raw.highlights) do target.highlights[#target.highlights + 1] = self:location(range, target) end
      end
      if raw.comparison then
        target.comparison = {
          left = self:location(raw.comparison.left), right = self:location(raw.comparison.right),
          diff = raw.comparison.diff == true,
        }
      end
    end
    for _, raw in ipairs(raw_steps) do
      local step = self:location(raw)
      if type(raw.description) ~= "string" or vim.trim(raw.description) == "" then fail("Step requires narration") end
      extras(step, raw)
      if raw.beats then
        if not vim.islist(raw.beats) or #raw.beats == 0 then fail("beats must be a non-empty array") end
        step.beats = {}
        for _, beat in ipairs(raw.beats) do
          local normalized = self:location(beat, step)
          if type(beat.description) ~= "string" or vim.trim(beat.description) == "" then fail("Beat requires narration") end
          extras(normalized, beat)
          step.beats[#step.beats + 1] = normalized
        end
      end
      steps[#steps + 1] = step
    end
    local seen = {}
    for _, step in ipairs(steps) do
      if step.id then
        integer(step.id, "id")
        if seen[step.id] then fail("Duplicate step ID") end
        if step.parent_id and not seen[step.parent_id] then fail("Parent must precede its children") end
        seen[step.id] = true
      elseif step.parent_id then fail("A parented step requires an ID") end
    end
    return steps
  end

  function self:effective(step)
    local beat = step.beats and step.beats[state.beat_index or 1]
    local effective = beat or step
    self:resolve(effective)
    return effective
  end

  function self:next_beat()
    local step = state.steps and state.steps[state.index]
    if step and step.beats and (state.beat_index or 1) < #step.beats then
      state.beat_index = (state.beat_index or 1) + 1
      return true
    end
    return false
  end

  function self:export(steps, root)
    local function resolve_all(loc)
      self:resolve(loc)
      for _, range in ipairs(loc.highlights or {}) do resolve_all(range) end
      for _, beat in ipairs(loc.beats or {}) do resolve_all(beat) end
      if loc.comparison then
        resolve_all(loc.comparison.left)
        resolve_all(loc.comparison.right)
      end
    end
    for _, step in ipairs(steps) do resolve_all(step) end
    if root then return require("naia.walkthrough.file").encode({ steps = steps }, root) end
    return { version = 2, steps = public(steps) }
  end

  function self:highlight(loc, win)
    local buf = self:resolve(loc)
    api.nvim_buf_set_extmark(buf, highlight_ns, loc.line_start - 1, 0, {
      end_row = loc.line_end, end_col = 0, hl_group = "AIWalkthroughStep", hl_eol = true,
      virt_text = loc.label and { { " " .. loc.label, "Title" } } or nil,
      priority = 210, strict = false,
    })
    if win then api.nvim_win_set_cursor(win, { loc.line_start, 0 }) end
  end

  function self:render(step, main_win)
    local comparison = step.comparison
    local visible = { [api.nvim_win_get_buf(main_win)] = true }
    if comparison then
      local right_buf, left_buf = self:resolve(comparison.right), self:resolve(comparison.left)
      local saved = { win = main_win, buf = api.nvim_win_get_buf(main_win), options = {},
        view = api.nvim_win_call(main_win, function() return vim.fn.winsaveview() end) }
      for _, name in ipairs(comparison_options) do
        saved.options[name] = api.nvim_get_option_value(name, { win = main_win })
      end
      self.borrowed_window = saved
      show_buffer(main_win, right_buf)
      local left_win = layout.without_equalizing(function()
        return api.nvim_open_win(left_buf, false, { split = "left", win = main_win })
      end)
      self.windows[#self.windows + 1] = left_win
      self.comparison_panes = { { win = left_win, loc = comparison.left }, { win = main_win, loc = comparison.right } }
      visible = { [left_buf] = true, [right_buf] = true }
      for _, pane in ipairs({ { loc = comparison.left, win = left_win, label = "Before" },
        { loc = comparison.right, win = main_win, label = "Current" } }) do
        self:highlight(pane.loc, pane.win)
        self:show_file_header(pane.win, pane.loc, pane.label)
        if comparison.diff then
          api.nvim_win_call(pane.win, function() vim.cmd("diffthis") end)
          -- Independent closed folds can collapse different numbers of screen
          -- rows. Keep walkthrough comparisons unfolded; restore the borrowed
          -- window's original folding options when leaving the comparison.
          vim.wo[pane.win].foldenable = false
        end
      end
    else
      self:show_file_header(main_win, step)
    end
    for _, range in ipairs(step.highlights or {}) do
      if not visible[self:resolve(range)] then fail("Highlights must target a visible comparison pane or the focused file") end
      self:highlight(range)
    end
    api.nvim_set_current_win(main_win)
    return comparison and comparison.right or step
  end

  function self:context(args)
    args = args or {}
    local current = state.steps and state.steps[state.index]
    if not state.active or not current then fail("No AI walkthrough is currently running") end
    local resolved, location_error = pcall(self.resolve, self, current)
    local effective_ok, effective = pcall(self.effective, self, current)
    local payload = {
      walkthrough_id = state.walkthrough_id, revision = state.revision,
      playback_revision = state.playback_revision,
      current_index = state.index, total_steps = #state.steps,
      recommended_parent_step_id = current.id, current_step = public(current),
      location_error = not resolved and tostring(location_error) or nil,
      playback = { paused = state.paused_for_continue, speaking = state.waiting_for_voice,
        finished = state.finished == true, beat_index = state.beat_index or 1,
        total_beats = current.beats and #current.beats or 1 },
    }
    if not effective_ok then payload.location_error = tostring(effective) end
    if args.include_tree then
      payload.ancestors, payload.siblings, payload.upcoming = {}, {}, {}
      local by_id = {}
      for _, step in ipairs(state.steps) do by_id[step.id] = step end
      local parent = by_id[current.parent_id]
      while parent do
        table.insert(payload.ancestors, 1, public(parent))
        parent = by_id[parent.parent_id]
      end
      for i, step in ipairs(state.steps) do
        if step.parent_id == current.parent_id and step.id ~= current.id then
          payload.siblings[#payload.siblings + 1] = public(step)
        end
        if i > state.index and #payload.upcoming < 5 then payload.upcoming[#payload.upcoming + 1] = public(step) end
      end
    end
    if args.include_buffer and effective_ok then
      effective = effective.comparison and effective.comparison.right or effective
      local buf = self:resolve(effective)
      local padding = math.min(100, math.max(0, tonumber(args.context_lines) or 5))
      local first = math.max(1, effective.line_start - padding)
      local last = math.min(api.nvim_buf_line_count(buf), effective.line_end + padding, first + 499)
      payload.buffer = { path = effective.path, line_start = first, line_end = last,
        text = lines_text(buf, first, last), modified = vim.bo[buf].modified,
        changedtick = api.nvim_buf_get_changedtick(buf) }
    end
    if args.include_editor then
      local buf = api.nvim_get_current_buf()
      payload.editor = { path = api.nvim_buf_get_name(buf), cursor = api.nvim_win_get_cursor(0), mode = vim.fn.mode() }
      local mode = vim.fn.mode()
      if mode == "v" or mode == "V" or mode == "\22" then
        local start = vim.fn.getpos("v")
        local cursor = api.nvim_win_get_cursor(0)
        local first, last = math.min(start[2], cursor[1]), math.max(start[2], cursor[1])
        payload.editor.selection = { line_start = first, line_end = last,
          anchor_column = start[3], cursor_column = cursor[2] + 1,
          text = lines_text(buf, first, math.min(last, first + 499)) }
      end
    end
    return payload
  end

  function self:guard(args)
    if not state.active then fail("No AI walkthrough is currently running") end
    if args.walkthrough_id ~= state.walkthrough_id then fail("Stale walkthrough_id; fetch current context") end
    if args.expected_revision ~= state.revision then fail("Stale revision; fetch current context") end
    if args.expected_current_step_id and state.steps[state.index].id ~= args.expected_current_step_id then
      fail("Playback moved to a different step; fetch current context")
    end
    if args.expected_playback_revision and args.expected_playback_revision ~= state.playback_revision then
      fail("Stale playback revision; fetch current context")
    end
  end

  function self:mutate(args, hooks)
    self:guard(args)
    local index
    for i, step in ipairs(state.steps) do if step.id == args.step_id then index = i end end
    if not index then fail("Unknown step_id") end
    local target = state.steps[index]
    local descendants = { [target.id] = true }
    for _, step in ipairs(state.steps) do
      if descendants[step.parent_id] then descendants[step.id] = true end
    end
    local operation = args.operation
    local additions
    if operation == "append_children" or operation == "replace_children" then
      additions = self:normalize(args.steps)
      additions = hooks.prepare(additions, target.id)
    elseif operation == "update_step" then
      additions = self:normalize({ args.step })
      additions[1].id, additions[1].parent_id = target.id, target.parent_id
    elseif operation ~= "remove_branch" then fail("Unknown mutation operation") end
    local active = state.steps[state.index]
    if (operation == "remove_branch" or operation == "replace_children") and descendants[active.id]
      and (operation == "remove_branch" or active.id ~= target.id) then
      fail("Cannot remove the active step; navigate elsewhere first")
    end
    local combined, inserted = {}, false
    local function append()
      for _, step in ipairs(additions or {}) do combined[#combined + 1] = step end
      inserted = true
    end
    for i, step in ipairs(state.steps) do
      if operation == "append_children" and not inserted and i > index and not descendants[step.id] then append() end
      if step.id == target.id then
        if operation == "update_step" then combined[#combined + 1] = additions[1]
        elseif operation ~= "remove_branch" then combined[#combined + 1] = step end
        if operation == "replace_children" then append() end
      elseif not ((operation == "replace_children" or operation == "remove_branch") and descendants[step.id]) then
        combined[#combined + 1] = step
      end
    end
    if operation == "append_children" and not inserted then append() end
    if #combined == 0 then fail("Cannot remove every step") end
    -- Only commit after complete validation. Mutation does not start or interrupt playback.
    state.steps = combined
    for i, step in ipairs(combined) do if step.id == active.id then state.index = i end end
    self:touch()
    hooks.changed(combined)
    local ids = {}
    for _, step in ipairs(additions or {}) do ids[#ids + 1] = step.id end
    return { walkthrough_id = state.walkthrough_id, revision = state.revision, step_ids = ids }
  end

  self.public = public
  return self
end

local function object(properties, required)
  -- Empty Lua tables encode as arrays; JSON Schema requires an object here.
  if next(properties) == nil then properties = vim.empty_dict() end
  return { type = "object", properties = properties, required = required, additionalProperties = false }
end
local function array(items) return { type = "array", items = items, minItems = 1 } end

function M.register(engine, naia, hooks)
  local string_field = { type = "string" }
  local number_field = { type = "integer", minimum = 1, description = "Positive integer (1 or greater)." }
  local boolean_field = { type = "boolean" }
  local location = object({ path = string_field, line_start = number_field, line_end = number_field,
    label = { type = "string", description = "Visible range or comparison pane label." }, anchor = object({ snippet = string_field, symbol = string_field,
      before = string_field, after = string_field }), revision = string_field, text = string_field,
  }, { "path", "line_start", "line_end" })
  local beat = vim.deepcopy(location)
  beat.properties.description = string_field
  beat.properties.highlights = array(location)
  beat.properties.comparison = object({ left = location, right = location, diff = boolean_field }, { "left", "right" })
  beat.required = { "description" }
  local step = vim.deepcopy(beat)
  step.required = { "path", "line_start", "line_end", "description" }
  step.properties.beats = array(beat)
  local guard = { walkthrough_id = string_field, expected_revision = number_field,
    expected_current_step_id = number_field, expected_playback_revision = number_field }
  local function register(name, description, schema, callback)
    naia.deregister_tool(name)
    local args = vim.deepcopy(schema.properties)
    for key, field in pairs(args) do field.required = vim.tbl_contains(schema.required or {}, key) end
    local ok, err = naia.register_tool(name, { description = description, args = args,
      callback = function(args) return vim.json.encode(callback(args or {})) end })
    if not ok then fail("Failed to register " .. name .. ": " .. tostring(err)) end
  end
  register("walkthrough_file_spec", "Get the portable walkthrough file JSON Schema, path rules, semantics and example. Use this before authoring a shareable walkthrough file yourself.",
    object({}), function() return require("naia.walkthrough.file").spec() end)
  register("walkthrough_export", "Export the current or last tour as portable version-3 JSON with repo-relative paths. Rejects locations outside root. Does not overwrite unless overwrite=true. Review embedded narration and snapshot text before sharing.",
    object({ path = string_field, root = string_field, overwrite = boolean_field }, { "path" }), hooks.export)
  register("walkthrough_load", "Load a portable JSON walkthrough file. By default returns status=awaiting_confirmation immediately while a non-blocking editor prompt waits for the user; fetch current_context after the user starts. Paths resolve against the current Git root (cwd outside Git), or explicit root, never the file directory. Set confirm_start=false ONLY when the user explicitly requests skipping confirmation. Legacy YAML/version-2 files remain supported for trusted local use.",
    object({ path = string_field, root = string_field, pause_after_step = boolean_field,
      confirm_start = { type = "boolean", default = true } }, { "path" }), hooks.load)
  register("walkthrough_start", "Start a narrated Neovim code tour. By default returns status=awaiting_confirmation immediately while a non-blocking editor prompt lets the user begin when ready. This is not an active tour yet; fetch current_context after the user starts for its ID and step IDs. You may set confirm_start=false to auto-start without confirmation ONLY if the user explicitly asks to auto-start or skip confirmation. Supports stable snippet/symbol anchors, synchronized narration beats, labeled highlights, and read-only side-by-side comparisons. Never applies code edits.",
    object({ steps = array(step), pause_after_step = boolean_field, confirm_start = {
      type = "boolean", default = true,
      description = "Show a confirmation box before starting (default true). Set false only when the user explicitly requests auto-start or no confirmation.",
    } }, { "steps" }), function(args)
      local started, reason = hooks.start(args.steps, {
        pause_after_step = args.pause_after_step == true, confirm_start = args.confirm_start ~= false,
      })
      if reason == "awaiting_confirmation" then
        return { status = "awaiting_confirmation", total_steps = #args.steps }
      end
      if not started then
        if reason == "cancelled" then return { cancelled = true } end
        fail("Could not start walkthrough")
      end
      local ids = {}
      for _, entry in ipairs(engine.state.steps) do ids[#ids + 1] = entry.id end
      return { walkthrough_id = engine.state.walkthrough_id, revision = engine.state.revision, step_ids = ids }
    end)
  register("walkthrough_current_context", "Get active step and stable mutation targets. Optionally include the tree neighborhood, live buffer text (including unsaved edits), and cursor/visual selection. Fetch before editing a walkthrough.",
    object({ include_tree = boolean_field, include_buffer = boolean_field, include_editor = boolean_field,
      context_lines = { type = "integer", description = "Context lines, from 0 to 100.", minimum = 0, maximum = 100 } }), function(args) return engine:context(args) end)
  local mutation = vim.tbl_extend("force", vim.deepcopy(guard), {
    operation = { type = "string", description = "One of: append_children, replace_children, update_step, remove_branch." },
    step_id = number_field, steps = array(step), step = step,
  })
  register("walkthrough_edit", "Atomically edit a walkthrough without changing playback. Append preserves existing branches; replacement explicitly removes descendants. Requires walkthrough_id and expected_revision from context. Returns stable step IDs. Navigate away before deleting the active step.",
    object(mutation, { "walkthrough_id", "expected_revision", "operation", "step_id" }),
    function(args) return engine:mutate(args, hooks) end)
  register("walkthrough_insert_steps", "Append follow-up steps under an existing step without replacing earlier explorations or starting playback. Fetch current context first and supply its walkthrough_id, revision, and explicit parent_step_id. Use nvim_walkthrough_control separately to play the new branch.",
    object(vim.tbl_extend("force", vim.deepcopy(guard), { parent_step_id = number_field, steps = array(step) }),
      { "walkthrough_id", "expected_revision", "parent_step_id", "steps" }), function(args)
      args.operation, args.step_id = "append_children", args.parent_step_id
      return engine:mutate(args, hooks)
    end)
  register("walkthrough_control", "Explicitly pause, resume, stop, or jump to a stable step ID. Guarded independently from edits so late follow-ups cannot hijack playback. Supply expected_current_step_id to reject requests after navigation.",
    object(vim.tbl_extend("force", vim.deepcopy(guard), {
      action = { type = "string", description = "One of: pause, resume, stop, jump." }, step_id = number_field,
    }), { "walkthrough_id", "expected_revision", "expected_current_step_id", "expected_playback_revision", "action" }), function(args)
      engine:guard(args)
      if args.expected_playback_revision == nil then fail("expected_playback_revision is required for playback control") end
      if args.action == "jump" then
        local index
        for i, entry in ipairs(engine.state.steps) do if entry.id == args.step_id then index = i end end
        if not index then fail("Unknown step_id") end
        hooks.jump(index)
      elseif args.action == "pause" then hooks.pause()
      elseif args.action == "resume" then hooks.resume()
      elseif args.action == "stop" then hooks.stop()
      else fail("Unknown playback action") end
      return { ok = true }
    end)
end

return M
