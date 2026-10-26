-- The step browser owns only its float, cursor mapping and user actions.
-- Playback supplies mutation/interrupt hooks instead of exposing global state.
local M = {}
local api = vim.api
local tree = require("naia.walkthrough.tree")

local function shorten(path)
  path = tostring(path or "")
  local separator = path:match("\\") and "\\" or "/"
  local parts = vim.split(path, separator, { plain = true, trimempty = false })
  if #parts <= 4 then return path end
  return table.concat({ parts[1], "...", parts[#parts - 2], parts[#parts - 1], parts[#parts] }, separator)
end

function M.new(state, hooks)
  local self, mapping = {}, {}

  function self.close()
    if state.browser_window and api.nvim_win_is_valid(state.browser_window) then
      pcall(api.nvim_win_close, state.browser_window, true)
    end
    if state.browser_buffer and api.nvim_buf_is_valid(state.browser_buffer) then
      pcall(api.nvim_buf_delete, state.browser_buffer, { force = true })
    end
    state.browser_window, state.browser_buffer, mapping = nil, nil, {}
  end

  local function focus()
    if state.browser_window and api.nvim_win_is_valid(state.browser_window) then
      pcall(api.nvim_set_current_win, state.browser_window)
    end
  end

  function self.render()
    if not (state.browser_buffer and api.nvim_buf_is_valid(state.browser_buffer)) then return false end
    local lines = { "AI Walkthrough",
      "<Enter>: jump  d/x: delete step tree  D: delete sibling group  Q: end walkthrough  q: close", "" }
    mapping = {}
    local target = 4
    for index, step in ipairs(state.steps or {}) do
      local summary = vim.trim(step.description):gsub("%s+", " ")
      if vim.fn.strdisplaywidth(summary) > 72 then summary = vim.fn.strcharpart(summary, 0, 69) .. "..." end
      lines[#lines + 1] = string.format("%s%s[%d] %s %s:%d - %s", index == state.index and ">" or " ",
        string.rep("  ", tree.depth(state.steps, index)), index, step.parent_id and "Child" or "Step",
        shorten(step.path), step.line_start, summary)
      mapping[#lines] = index
      if index == state.index then target = #lines end
    end
    vim.bo[state.browser_buffer].modifiable = true
    api.nvim_buf_set_lines(state.browser_buffer, 0, -1, false, lines)
    vim.bo[state.browser_buffer].modifiable = false
    if state.browser_window and api.nvim_win_is_valid(state.browser_window) then
      pcall(api.nvim_win_set_cursor, state.browser_window, { target, 0 })
    end
    return true
  end

  local function selected()
    if not (state.browser_window and api.nvim_win_is_valid(state.browser_window)) then return nil end
    return mapping[api.nvim_win_get_cursor(state.browser_window)[1]]
  end

  local function delete(siblings)
    local index = selected()
    local step = index and (state.steps or {})[index]
    if not step then return end
    hooks.interrupt()
    local ids = {}
    for _, candidate in ipairs(state.steps) do
      if (siblings and candidate.parent_id == step.parent_id) or (not siblings and candidate.id == step.id) then
        for id in pairs(tree.descendants(state.steps, candidate.id)) do ids[id] = true end
      end
    end
    local remaining, next_index, removed = hooks.remove(ids)
    if removed == 0 then return end
    if not remaining or #remaining == 0 then
      hooks.stop(false)
      vim.notify(siblings and "Removed the final AI walkthrough sibling group"
        or "Removed the final AI walkthrough step tree", vim.log.levels.INFO)
      return
    end
    self.render()
    if next_index then hooks.jump(next_index); self.render() end
    focus()
    vim.notify(string.format("Removed %d AI walkthrough %sstep%s", removed, siblings and "sibling " or "",
      removed == 1 and "" or "s"), vim.log.levels.INFO)
  end

  function self.open()
    if not state.active or not state.steps or #state.steps == 0 then
      vim.notify("No AI walkthrough is currently running", vim.log.levels.WARN)
      return
    end
    if state.browser_window and api.nvim_win_is_valid(state.browser_window) then self.render(); focus(); return end
    local buf = api.nvim_create_buf(false, true)
    local width = math.max(60, math.min(vim.o.columns - 2, math.floor(vim.o.columns * 0.95)))
    local height = math.min(math.max(10, #state.steps + 4), math.max(8, vim.o.lines - 6))
    local ok, win = pcall(api.nvim_open_win, buf, true, {
      relative = "editor", row = math.max(1, math.floor((vim.o.lines - height) / 2) - 1),
      col = math.max(1, math.floor((vim.o.columns - width) / 2)), width = width, height = height,
      style = "minimal", border = "rounded", title = " AI Walkthrough ", title_pos = "center", zindex = 200,
    })
    if not ok then pcall(api.nvim_buf_delete, buf, { force = true }); return end
    state.browser_buffer, state.browser_window = buf, win
    vim.bo[buf].bufhidden, vim.bo[buf].buftype, vim.bo[buf].swapfile = "wipe", "nofile", false
    vim.bo[buf].modifiable, vim.wo[win].cursorline, vim.wo[win].wrap = false, true, false
    local opts = { buffer = buf, silent = true, nowait = true }
    for _, key in ipairs({ "q", "<Esc>" }) do vim.keymap.set("n", key, self.close, opts) end
    vim.keymap.set("n", "Q", function()
      local tour, revision = state.walkthrough_id, state.playback_revision
      self.close()
      vim.schedule(function()
        if state.walkthrough_id == tour and state.playback_revision == revision then hooks.stop(true) end
      end)
    end, opts)
    vim.keymap.set("n", "<CR>", function()
      local index = selected()
      if index then self.close(); hooks.jump(index) end
    end, opts)
    for _, key in ipairs({ "d", "x", "dd" }) do vim.keymap.set("n", key, function() delete(false) end, opts) end
    vim.keymap.set("n", "D", function() delete(true) end, opts)
    self.render()
  end

  return self
end

return M
