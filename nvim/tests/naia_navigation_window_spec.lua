-- Run: nvim --headless -u NONE -l tests/naia_navigation_window_spec.lua
local file = debug.getinfo(1, "S").source:sub(2)
vim.opt.rtp:prepend(vim.fn.fnamemodify(file, ":p:h:h"))
vim.g.naia_ui_policies = { navigation = "ask" }
local ui = require("naia.ui")
local target = vim.fn.tempname()
vim.fn.writefile({ "first", "second" }, target)
target = assert(vim.uv.fs_realpath(target))

local function request()
  return ui.request("navigation", { path = target, line = 2, reason = "Test editing window fallback" })
end

local function navigate()
  local result = request()
  ui.approve(result.request_id)
  local status = ui.status(result.request_id)
  assert(status.status == "applied", vim.inspect(status))
  assert(vim.api.nvim_buf_get_name(0) == target)
  assert(vim.api.nvim_win_get_cursor(0)[1] == 2)
  assert(vim.bo.buftype == "" and vim.api.nvim_win_get_config(0).relative == "")
end

for _, kind in ipairs({ "nofile", "terminal", "locked" }) do
  vim.cmd("tabnew")
  local anchor, buffer = vim.api.nvim_get_current_win(), vim.api.nvim_get_current_buf()
  local job
  if kind == "terminal" then
    job = vim.fn.jobstart({ "cat" }, { term = true })
    assert(job > 0)
  elseif kind == "locked" then
    vim.wo.winfixbuf = true
  else
    vim.bo.buftype = "nofile"
    vim.api.nvim_buf_set_lines(buffer, 0, -1, false, { "unsaved scratch work" })
  end
  local original = vim.api.nvim_buf_get_lines(buffer, 0, -1, false)
  -- A request must not create a split until it is approved.
  local pending = request()
  assert(#vim.api.nvim_tabpage_list_wins(0) == 1)
  ui.reject(pending.request_id)
  assert(#vim.api.nvim_tabpage_list_wins(0) == 1)
  -- Floating UI must not become the destination or split anchor.
  local floating = vim.api.nvim_open_win(vim.api.nvim_create_buf(false, true), true, {
    relative = "editor", row = 0, col = 0, width = 20, height = 2,
  })
  navigate()
  local destination = vim.api.nvim_get_current_win()
  assert(destination ~= anchor and destination ~= floating)
  assert(vim.api.nvim_win_get_buf(anchor) == buffer, "replaced the original buffer")
  if kind ~= "terminal" then
    assert(vim.deep_equal(vim.api.nvim_buf_get_lines(buffer, 0, -1, false), original))
  end
  assert(vim.wo[anchor].winfixbuf == (kind == "locked"), "changed original buffer lock")
  local count = #vim.api.nvim_tabpage_list_wins(0)
  vim.api.nvim_set_current_win(floating)
  navigate()
  assert(vim.api.nvim_get_current_win() == destination, "did not reuse editing window")
  assert(#vim.api.nvim_tabpage_list_wins(0) == count, "created an unnecessary split")
  if job then vim.fn.jobstop(job) end
  vim.cmd("tabclose!")
end
vim.fn.delete(target)
print("PASS: navigation creates editing splits only after approval, preserves special/locked buffers, and reuses editing windows")
