local M = {}
local style = require("naia_ui_style")
local window, buffer, shown_id, origin
-- This also supports moving a live prompt without disturbing its request,
-- callbacks, or focus. Leave the command line and statusline unobstructed.
function M.position(target)
  if not vim.api.nvim_win_is_valid(target) then return end
  local width = math.min(64, vim.o.columns - 4)
  local bottom = vim.o.lines - vim.o.cmdheight - (vim.o.laststatus == 0 and 0 or 1)
  if width < 28 or bottom < 8 then return end
  vim.api.nvim_win_set_config(target, {
    relative = "editor", anchor = "SW", row = bottom - 1,
    col = math.floor((vim.o.columns - width - 2) / 2), width = width,
    height = math.min(vim.api.nvim_buf_line_count(vim.api.nvim_win_get_buf(target)), bottom - 3),
  })
  vim.w[target].naia_approval = true
end

function M.close()
  local focused = window and vim.api.nvim_get_current_win() == window
  if window and vim.api.nvim_win_is_valid(window) then vim.api.nvim_win_close(window, true) end
  if buffer and vim.api.nvim_buf_is_valid(buffer) then vim.api.nvim_buf_delete(buffer, { force = true }) end
  window, buffer, shown_id = nil, nil, nil
  if focused and origin and vim.api.nvim_win_is_valid(origin)
      and vim.api.nvim_win_get_tabpage(origin) == vim.api.nvim_get_current_tabpage() then
    vim.api.nvim_set_current_win(origin)
  end
end

function M.show(request, handlers, focus)
  if shown_id == request.id and window and vim.api.nvim_win_is_valid(window) and not focus then
    return
  end
  M.close()
  local width = math.min(64, vim.o.columns - 4)
  if width < 28 or vim.o.lines < 12 then
    vim.notify("Naia UI approval pending: :NaiaApprove " .. request.id .. " or :NaiaReject " .. request.id)
    return
  end
  origin = vim.api.nvim_get_current_win()
  local data = request.data
  local heading, subject
  if request.kind == "navigation" then
    heading = "Jump to " .. vim.fn.fnamemodify(data.path, ":t") .. ":" .. data.line .. "?"
    subject = vim.fn.fnamemodify(data.path, ":~:.")
  elseif request.kind == "quickfix" then
    heading = ({ new = "Create quickfix list?", append = "Append to quickfix?", replace = "Replace quickfix list?" })[data.mode]
    subject = string.format("%s · %d entries", data.title, #data.items)
  else
    heading = request.kind == "card" and "Show pinned card?" or "Show companion?"
    subject = data.title
  end
  local lines = { heading, subject, "Why: " .. request.reason:gsub("%s+", " "), "" }
  lines[#lines + 1] = "Focused: y allow · n/q reject"
  lines[#lines + 1] = ":NaiaApprove · :NaiaReject"
  -- This is a compact plain-text approval, never executable markup.
  local display = {}
  for _, line in ipairs(lines) do
    for _, part in ipairs(vim.split(line, "\n", { plain = true })) do
      part = part:gsub("[%z\1-\8\11-\31\127]", " "):gsub("\t", "  ")
      local clipped = part
      if vim.fn.strdisplaywidth(part) > width then
        clipped = vim.fn.strcharpart(part, 0, width - 1, 1)
        while vim.fn.strdisplaywidth(clipped .. "…") > width do
          clipped = vim.fn.strcharpart(clipped, 0, vim.fn.strchars(clipped, 1) - 1, 1)
        end
        clipped = clipped .. "…"
      end
      display[#display + 1] = clipped
    end
  end
  buffer = vim.api.nvim_create_buf(false, true)
  vim.bo[buffer].bufhidden = "wipe"
  vim.bo[buffer].swapfile = false
  vim.api.nvim_buf_set_lines(buffer, 0, -1, false, display)
  vim.bo[buffer].modified = false
  vim.bo[buffer].modifiable = false
  local bottom = vim.o.lines - vim.o.cmdheight - (vim.o.laststatus == 0 and 0 or 1)
  window = vim.api.nvim_open_win(buffer, focus == true, {
    relative = "editor", anchor = "SW", row = bottom - 1,
    col = math.floor((vim.o.columns - width - 2) / 2),
    width = width, height = math.min(#display, bottom - 3),
    border = "rounded", title = " N.A.I.A · Approval ", title_pos = "center",
    style = "minimal", focusable = true, zindex = 110,
  })
  M.position(window)
  style.apply(window, "approval")
  shown_id = request.id
  vim.wo[window].wrap = false
  local namespace = vim.api.nvim_create_namespace("naia_approval_style")
  vim.api.nvim_buf_set_extmark(buffer, namespace, 0, 0, {
    end_col = #display[1], hl_group = "NaiaUIApprovalTitle",
  })
  vim.api.nvim_buf_set_extmark(buffer, namespace, 2, 0, {
    end_col = #display[3], hl_group = "NaiaUIApprovalMuted",
  })
  for key, callback in pairs({ y = handlers.approve, n = handlers.reject, q = handlers.reject,
      a = handlers.allow, m = handlers.mute }) do
    vim.keymap.set("n", key, callback, { buffer = buffer, nowait = true, silent = true })
  end
end

vim.api.nvim_create_autocmd("VimResized", {
  group = vim.api.nvim_create_augroup("NaiaApprovalPosition", { clear = true }),
  callback = function()
    for _, target in ipairs(vim.api.nvim_list_wins()) do
      if vim.w[target].naia_approval then M.position(target) end
    end
  end,
})

return M
