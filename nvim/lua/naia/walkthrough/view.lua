-- Source-window selection, narration floats and active-range highlighting.
-- Playback owns transitions; this object only owns editor presentation.
local M = {}
local api = vim.api
local layout = require("naia.walkthrough.layout")
local paths = require("naia.walkthrough.paths")
local ns = api.nvim_create_namespace("ai_walkthrough_active_step")

function M.highlights()
  api.nvim_set_hl(0, "AIWalkthroughStep", { bg = "#16381e", fg = "NONE" })
  api.nvim_set_hl(0, "AIWalkthroughDescription", { bg = "#0f1720", fg = "#d7e3f4" })
  api.nvim_set_hl(0, "AIWalkthroughDescriptionBorder", { bg = "#0f1720", fg = "#4f6b8a" })
end

function M.buffer_path(buf)
  local name = api.nvim_buf_get_name(buf)
  return name ~= "" and paths.normalize(name) or nil
end

local function is_chat(buf)
  local ok, cc = pcall(require, "codecompanion")
  if not ok then return false end
  local found, chat = pcall(cc.buf_get_chat, buf)
  return found and chat ~= nil
end

function M.select(target)
  local same, unmodified, any
  local same_area, unmodified_area, any_area = -1, -1, -1
  for _, win in ipairs(api.nvim_tabpage_list_wins(0)) do
    local buf = api.nvim_win_get_buf(win)
    if vim.bo[buf].buftype == "" and not is_chat(buf) then
      local area = api.nvim_win_get_width(win) * api.nvim_win_get_height(win)
      if M.buffer_path(buf) == target and area > same_area then same, same_area = win, area end
      if not vim.bo[buf].modified and area > unmodified_area then unmodified, unmodified_area = win, area end
      if area > any_area then any, any_area = win, area end
    end
  end
  local win = same or unmodified or any
  if win then return win, false end
  local command = is_chat(api.nvim_get_current_buf()) and "botright vnew" or "vsplit"
  local ok = pcall(layout.without_equalizing, function() vim.cmd(command) end)
  return ok and api.nvim_get_current_win() or nil, ok
end

function M.capture(win)
  if not (win and api.nvim_win_is_valid(win)) then return nil end
  local ok, saved = pcall(api.nvim_win_call, win, vim.fn.winsaveview)
  return { window_id = win, buffer_id = api.nvim_win_get_buf(win), winbar = vim.wo[win].winbar,
    view = ok and saved or nil }
end

function M.restore(saved)
  if not (saved and saved.window_id and api.nvim_win_is_valid(saved.window_id)) then return false end
  local win = saved.window_id
  if saved.buffer_id and api.nvim_buf_is_valid(saved.buffer_id) then pcall(api.nvim_win_set_buf, win, saved.buffer_id) end
  if saved.winbar ~= nil then pcall(api.nvim_set_option_value, "winbar", saved.winbar, { win = win }) end
  if saved.view then pcall(api.nvim_win_call, win, function() vim.fn.winrestview(saved.view) end) end
  return true
end

function M.focus(win, first, last)
  if not (win and api.nvim_win_is_valid(win)) then return false end
  local count = math.max(1, api.nvim_buf_line_count(api.nvim_win_get_buf(win)))
  first = math.min(math.max(first, 1), count)
  last = math.min(math.max(last, first), count)
  local tall = last - first + 1 > math.max(1, vim.fn.getwininfo(win)[1].height)
  api.nvim_win_set_cursor(win, { tall and first or math.floor((first + last) / 2), 0 })
  api.nvim_win_call(win, function()
    -- Native centering accounts for folds, wrapping, diff filler and the winbar.
    vim.cmd("normal! zv")
    vim.cmd(tall and "normal! zt" or "normal! zz")
  end)
  return true
end

local function wrap(text, width)
  local lines = {}
  for _, paragraph in ipairs(vim.split(text, "\n", { plain = true, trimempty = false })) do
    local current = ""
    for word in paragraph:gmatch("%S+") do
      local candidate = current == "" and word or current .. " " .. word
      if vim.fn.strdisplaywidth(candidate) <= width then
        current = candidate
      else
        if current ~= "" then lines[#lines + 1] = current end
        if vim.fn.strdisplaywidth(word) <= width then current = word
        else
          local chunk = ""
          for _, char in ipairs(vim.split(word, "\\zs")) do
            local next_chunk = chunk .. char
            if vim.fn.strdisplaywidth(next_chunk) > width and chunk ~= "" then
              lines[#lines + 1], chunk = chunk, char
            else chunk = next_chunk end
          end
          current = chunk
        end
      end
    end
    if current ~= "" or paragraph == "" then lines[#lines + 1] = current end
  end
  return #lines > 0 and lines or { "" }
end

function M.new(state, engine)
  local self = {}

  function self.clear_highlight()
    if state.highlighted_buffer and api.nvim_buf_is_valid(state.highlighted_buffer) then
      pcall(api.nvim_buf_clear_namespace, state.highlighted_buffer, ns, 0, -1)
    end
    state.highlighted_buffer = nil
  end

  function self.close_description()
    if state.description_window and api.nvim_win_is_valid(state.description_window) then
      pcall(api.nvim_win_close, state.description_window, true)
    end
    if state.description_buffer and api.nvim_buf_is_valid(state.description_buffer) then
      pcall(api.nvim_buf_delete, state.description_buffer, { force = true })
    end
    state.description_window, state.description_buffer = nil, nil
  end

  function self.highlight(buf, first, last)
    self.clear_highlight()
    if not (buf and api.nvim_buf_is_valid(buf)) then return false end
    local count = math.max(1, api.nvim_buf_line_count(buf))
    pcall(api.nvim_buf_set_extmark, buf, ns, math.min(math.max(first, 1), count) - 1, 0, {
      end_row = math.min(math.max(last, first), count), end_col = 0,
      hl_group = "AIWalkthroughStep", hl_eol = true, priority = 200, strict = false,
    })
    state.highlighted_buffer = buf
    return true
  end

  local function description_lines(index, description, width)
    local steps = state.steps or {}
    local current = steps[index] or {}
    local lines = { string.format("Step %d/%d", index, #steps) }
    if current.parent_id then
      local parent, child_index, child_total = nil, 0, 0
      for i, step in ipairs(steps) do
        if step.id == current.parent_id then parent = i end
        if step.parent_id == current.parent_id then
          child_total = child_total + 1
          if step.id == current.id then child_index = child_total end
        end
      end
      if parent then
        lines[#lines + 1] = string.format("Parent: Step %d", parent)
        lines[#lines + 1] = string.format("Child Step %d/%d", child_index, child_total)
      end
    end
    lines[#lines + 1] = ""
    return vim.list_extend(lines, wrap(description, width))
  end

  function self.describe(win, index, first, description, step)
    self.close_description()
    if not (win and api.nvim_win_is_valid(win)) then return false end
    local width, height = api.nvim_win_get_width(win), api.nvim_win_get_height(win)
    if width - 4 < 16 or height < 3 then return false end
    local inner = math.min(math.max(24, math.floor(width * 0.35)), width - 4)
    local lines = description_lines(index, description, inner)
    local box_height = math.min(#lines, math.max(1, height - 2))
    local ok, pos = pcall(vim.fn.screenpos, win, first, 1)
    local origin = vim.fn.win_screenpos(win)
    local line_row
    if ok and type(pos) == "table" and pos.row and pos.row > 0 and type(origin) == "table" and origin[1] then
      line_row = math.max(0, pos.row - origin[1])
    else
      local saved = api.nvim_win_call(win, vim.fn.winsaveview)
      line_row = math.max(0, first - (saved.topline or 1))
    end
    local outer = box_height + 2
    local row = math.max(0, math.min(line_row - outer, math.max(0, height - outer)))
    local col, relative = math.max(0, width - inner - 2), "win"
    if step then
      local panes = engine.comparison_panes or { { win = win, loc = step } }
      local placement = layout.place(win, panes, step.highlights, box_height, row)
      if placement then
        row, box_height = placement.row, placement.height
        col, relative = col + placement.col_origin, "editor"
      end
    end
    local displayed = vim.list_slice(lines, 1, box_height)
    if box_height < #lines then
      displayed[box_height] = vim.fn.strcharpart(displayed[box_height] or "", 0, math.max(0, inner - 3)) .. "..."
    end
    local buf = api.nvim_create_buf(false, true)
    vim.bo[buf].bufhidden = "wipe"
    api.nvim_buf_set_lines(buf, 0, -1, false, displayed)
    vim.bo[buf].modifiable = false
    local opened, float = pcall(api.nvim_open_win, buf, false, {
      relative = relative, win = relative == "win" and win or nil, row = row, col = col,
      width = inner, height = box_height, style = "minimal", border = "rounded",
      focusable = false, noautocmd = true, zindex = 150,
    })
    if not opened then pcall(api.nvim_buf_delete, buf, { force = true }); return false end
    vim.wo[float].wrap = true
    vim.wo[float].winhl = "Normal:AIWalkthroughDescription,NormalFloat:AIWalkthroughDescription,FloatBorder:AIWalkthroughDescriptionBorder"
    state.description_buffer, state.description_window = buf, float
    return true
  end

  return self
end

return M
