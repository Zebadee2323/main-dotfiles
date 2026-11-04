-- Walkthrough split isolation and screen-space narration placement.
local M = {}
local api = vim.api

-- Split creation/removal normally equalizes unrelated windows when 'ea' is
-- set. Restrict the space change to the split's own frame, without changing
-- the user's option beyond this synchronous operation (including failures).
function M.without_equalizing(callback)
  local equalalways = vim.o.equalalways
  vim.o.equalalways = false
  local result = { pcall(callback) }
  -- Re-enabling 'ea' itself equalizes the tab. Preserve the post-operation
  -- geometry so restoring the option does not undo the local-only split.
  local sizes = vim.fn.winrestcmd()
  vim.o.equalalways = equalalways
  if equalalways then vim.cmd(sizes) end
  if not result[1] then error(result[2], 0) end
  return unpack(result, 2)
end

-- Rows are zero-based, relative to the available text viewport. Values mean
-- padding (1), changed/filler rows (2), or the explained range (3).
function M.choose(height, requested_height, rows, preferred_row)
  local desired = math.min(requested_height, height - 2)
  if desired < 1 then return nil end
  local minimum = math.min(desired, 3)
  local best
  local function better(candidate, previous)
    if not previous then return true end
    for _, key in ipairs({ "focus", "changes", "padding" }) do
      if candidate[key] ~= previous[key] then return candidate[key] < previous[key] end
    end
    if candidate.height ~= previous.height then return candidate.height > previous.height end
    return candidate.distance < previous.distance
  end
  for inner_height = desired, minimum, -1 do
    local clear
    for row = 0, height - inner_height - 2 do
      local candidate = { row = row, height = inner_height, focus = 0, changes = 0, padding = 0,
        distance = math.abs(row - preferred_row) }
      -- Include both border rows, not just the text inside the float.
      for occupied = row, row + inner_height + 1 do
        local kind = rows[occupied]
        if kind == 3 then candidate.focus = candidate.focus + 1
        elseif kind == 2 then candidate.changes = candidate.changes + 1
        elseif kind == 1 then candidate.padding = candidate.padding + 1 end
      end
      if better(candidate, best) then best = candidate end
      if candidate.focus + candidate.changes + candidate.padding == 0
        and (not clear or candidate.distance < clear.distance) then clear = candidate end
    end
    -- Prefer a full-size clear gap, then the largest clear shortened box.
    if clear then return clear end
  end
  return best
end

function M.place(main_win, panes, highlights, requested_height, preferred_row)
  -- Placement never scrolls, resizes, or edits the code panes.
  -- screenpos/diff_hlID must see the final diff filler and scroll positions.
  vim.cmd("redraw")
  local info = vim.fn.getwininfo(main_win)[1]
  if not info then return nil end
  local top = info.winrow - 1 + (info.winbar or 0)
  local rows = {}
  local function mark(first, last, kind)
    for absolute = math.max(top, first - 1), math.min(top + info.height - 1, last + 1) do
      local relative = absolute - top
      local value = absolute >= first and absolute <= last and kind or 1
      rows[relative] = math.max(rows[relative] or 0, value)
    end
  end
  for _, pane in ipairs(panes) do
    if api.nvim_win_is_valid(pane.win) then
      api.nvim_win_call(pane.win, function()
        local pane_info = vim.fn.getwininfo(pane.win)[1]
        local buf = api.nvim_win_get_buf(pane.win)
        local last_line = math.min(api.nvim_buf_line_count(buf), pane_info.botline)
        for line = pane_info.topline, last_line do
          local pos = vim.fn.screenpos(pane.win, line, 1)
          if pos.row > 0 then
            local first = pos.row - 1
            local text = api.nvim_buf_get_lines(buf, line - 1, line, false)[1] or ""
            local ending = vim.fn.screenpos(pane.win, line, #text + 1)
            local last = math.max(first, ending.row - 1)
            if vim.wo[pane.win].diff then
              if vim.fn.diff_hlID(line, 1) ~= 0 then mark(first, last, 2) end
              local filler = vim.fn.diff_filler(line)
              if filler > 0 then mark(first - filler, first - 1, 2) end
            end
            if line >= pane.loc.line_start and line <= pane.loc.line_end then mark(first, last, 3) end
            for _, range in ipairs(highlights or {}) do
              if range._buffer == buf and line >= range.line_start and line <= range.line_end then mark(first, last, 3) end
            end
          end
        end
      end)
    end
  end
  -- preferred_row used the old window-relative origin, which includes winbar.
  local selected = M.choose(info.height, requested_height, rows, preferred_row - (info.winbar or 0))
  if selected then
    selected.row = selected.row + top
    selected.col_origin = info.wincol - 1
  end
  return selected
end

return M
