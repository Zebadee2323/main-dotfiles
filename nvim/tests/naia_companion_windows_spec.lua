-- Run: nvim --headless -u NONE -l tests/naia_companion_windows_spec.lua
local file = debug.getinfo(1, "S").source:sub(2)
vim.opt.rtp:prepend(vim.fn.fnamemodify(file, ":p:h:h"))
local ui = require("naia.ui")
local actions = require("naia.ui.actions")
local function companion(title)
  local request = ui.request("companion", { title = title, content = "Explanation", reason = "Test window selection" })
  assert(vim.wait(1000, function() return ui.status(request.request_id).status ~= "pending" end, 10))
  local status = ui.status(request.request_id)
  assert(status.status == "applied", vim.inspect(status))
  return status.result
end
local function verify(source, title)
  local original = vim.api.nvim_win_get_buf(source)
  local focused = vim.api.nvim_get_current_win()
  local first = companion(title)
  assert(vim.api.nvim_get_current_win() == focused, "companion stole focus")
  assert(vim.api.nvim_win_get_buf(source) == original, "companion replaced the source buffer")
  assert(vim.api.nvim_win_get_config(first.window).relative == "", "companion must be a split")
  assert(vim.bo[first.buffer].buflisted and not vim.bo[first.buffer].modifiable)
  local second = companion(title .. " again")
  assert(first.window == second.window and first.buffer ~= second.buffer, "companion did not reuse its split")
  vim.api.nvim_win_close(second.window, false)
  return second.buffer
end

-- A normal window in another tab must not be required or borrowed.
local other_tab = vim.api.nvim_get_current_tabpage()
vim.cmd("tabnew")
local source = vim.api.nvim_get_current_win()
vim.bo.buftype = "nofile"
vim.api.nvim_buf_set_lines(0, 0, -1, false, { "Special buffer content" })
local history = verify(source, "Only nofile")
assert(vim.api.nvim_get_current_tabpage() ~= other_tab)
assert(vim.api.nvim_buf_get_lines(0, 0, -1, false)[1] == "Special buffer content")
local reopened = actions.open_companion(false)
assert(vim.api.nvim_win_get_buf(reopened) == history, "could not reopen hidden history")
assert(vim.api.nvim_get_current_win() == source)
vim.api.nvim_win_close(reopened, false)

-- A focused float must not become the split anchor or lose focus.
local float = vim.api.nvim_open_win(vim.api.nvim_create_buf(false, true), true, {
  relative = "editor", row = 1, col = 1, width = 20, height = 3,
})
verify(source, "Focused float over nofile")
assert(vim.api.nvim_get_current_win() == float)
vim.api.nvim_win_close(float, true)

vim.cmd("enew")
local terminal = vim.api.nvim_get_current_buf()
vim.fn.jobstart({ vim.o.shell, "-c", "exit 0" }, { term = true })
assert(vim.bo[terminal].buftype == "terminal")
verify(source, "Only terminal")

-- Exercise wide/right placement as well as narrow/below placement.
vim.o.columns = 160
verify(source, "Wide terminal tab")

-- Even when a special buffer is current, prefer a normal editing window.
vim.cmd("vnew")
local editing = vim.api.nvim_get_current_win()
vim.o.columns = 80
vim.api.nvim_set_current_win(source)
local result = companion("Prefer editing window")
local layout = vim.fn.winlayout()
local function siblings(node)
  if node[1] == "leaf" then return false end
  local leaves = {}
  for _, child in ipairs(node[2]) do
    if child[1] == "leaf" then leaves[child[2]] = true end
    if siblings(child) then return true end
  end
  return leaves[editing] and leaves[result.window] or false
end
assert(siblings(layout), "companion did not split the normal editing window")
assert(vim.api.nvim_get_current_win() == source)
vim.api.nvim_win_close(result.window, false)
vim.api.nvim_win_close(editing, false)

-- After companion cleanup, navigation creates its own editing split rather
-- than replacing the terminal or borrowing the normal window in another tab.
local target = vim.fn.tempname()
vim.fn.writefile({ "first", "target" }, target)
target = assert(vim.uv.fs_realpath(target))
local tab = vim.api.nvim_get_current_tabpage()
local windows = #vim.api.nvim_tabpage_list_wins(tab)
local data = actions.prepare("navigation", { path = target, line = 2, reason = "Test navigation fallback" })
assert(#vim.api.nvim_tabpage_list_wins(tab) == windows, "preparing navigation created a split")
actions.apply("navigation", data)
local destination = vim.api.nvim_get_current_win()
assert(vim.api.nvim_get_current_tabpage() == tab, "navigation borrowed another tab")
assert(destination ~= source, "navigation reused the terminal window")
assert(#vim.api.nvim_tabpage_list_wins(tab) == windows + 1, "navigation did not create one editing split")
assert(vim.api.nvim_win_get_config(destination).relative == "", "navigation destination must be a split")
assert(vim.bo.buftype == "" and not vim.wo.winfixbuf, "navigation destination must be editable")
assert(vim.api.nvim_buf_get_name(0) == target, "navigation opened the wrong file")
assert(vim.api.nvim_win_get_cursor(0)[1] == 2, "navigation did not select the requested line")
assert(vim.api.nvim_win_get_buf(source) == terminal, "navigation replaced the terminal buffer")
assert(vim.bo[terminal].buftype == "terminal", "navigation changed the terminal buffer type")
vim.api.nvim_win_close(destination, false)
vim.fn.delete(target)
print("PASS: companion special-buffer fallback, float focus, split reuse/history, editing preference, safe navigation split")
