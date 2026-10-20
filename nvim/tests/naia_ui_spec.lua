-- Run: nvim --headless -u NONE -l tests/naia_ui_spec.lua
local file = debug.getinfo(1, "S").source:sub(2)
vim.opt.rtp:prepend(vim.fn.fnamemodify(file, ":p:h:h"))
local ui = require("naia_ui")
local actions = require("naia_ui_actions")
local warnings = {}
vim.notify = function(message) warnings[#warnings + 1] = message end
local function flush() vim.wait(40, function() return false end, 5) end
local temporary = vim.fn.tempname()
local directory = (vim.uv or vim.loop).fs_realpath(vim.fs.dirname(temporary)) or vim.fs.dirname(temporary)
local root = vim.fs.joinpath(directory, vim.fs.basename(temporary))
local origin, target = root .. "-origin.txt", root .. "-|let g:injected=1|#%.txt"
vim.fn.writefile({ "origin one", "origin two", "origin three" }, origin)
vim.fn.writefile({ "target one", "target two", "target three", "target four" }, target)
vim.api.nvim_cmd({ cmd = "edit", args = { origin } }, {})
vim.api.nvim_win_set_cursor(0, { 2, 0 })
local source = vim.api.nvim_get_current_win()
local function status(request) return ui.status(request.request_id) end
local function nav()
  return ui.request("navigation", { path = target, line = 3, column = 2, reason = "Test navigation" })
end
local function mapping(buffer, lhs)
  for _, map in ipairs(vim.api.nvim_buf_get_keymap(buffer, "n")) do
    if map.lhs == lhs then return map.callback() end
  end
  error("Missing mapping " .. lhs)
end
local function press_button(label)
  local prompt
  for _, win in ipairs(vim.api.nvim_list_wins()) do
    if vim.w[win].naia_approval then prompt = win end
  end
  assert(prompt, "no approval to click")
  local footer = vim.api.nvim_buf_get_lines(vim.api.nvim_win_get_buf(prompt), 4, 5, false)[1]
  local column = assert(footer:find(label, 1, true))
  vim.api.nvim_set_current_win(prompt)
  vim.api.nvim_win_set_cursor(prompt, { 5, column - 1 })
  mapping(vim.api.nvim_win_get_buf(prompt), "<CR>")
  flush()
end

local request = nav()
assert(request.status == "pending")
flush()
assert(vim.api.nvim_get_current_win() == source and vim.api.nvim_buf_get_name(0) == origin,
  "approval stole focus or navigated early")
local function assert_approval_position()
  local found = false
  for _, win in ipairs(vim.api.nvim_list_wins()) do
    if vim.w[win].naia_approval then
      local config = vim.api.nvim_win_get_config(win)
      local bottom = vim.o.lines - vim.o.cmdheight - (vim.o.laststatus == 0 and 0 or 1)
      assert(config.anchor == "SW" and config.row == bottom - 1, "approval is not bottom-aligned")
      assert(config.col == math.floor((vim.o.columns - config.width - 2) / 2), "approval is not centred")
      if not vim.w[win].naia_approval_review then
        assert(config.height == 5 and config.width <= 64, "passive approval should be compact")
        local lines = vim.api.nvim_buf_get_lines(vim.api.nvim_win_get_buf(win), 0, -1, false)
        assert(lines[1]:find("Jump to ", 1, true))
        assert(lines[5] == "[Allow]  [Reject]  [Details]")
      end
      found = true
    end
  end
  assert(found, "approval window missing")
end
assert_approval_position()
local old_columns, old_lines = vim.o.columns, vim.o.lines
vim.o.columns, vim.o.lines = 100, 30
vim.api.nvim_exec_autocmds("VimResized", {})
assert_approval_position()
vim.o.columns, vim.o.lines = old_columns, old_lines
vim.api.nvim_exec_autocmds("VimResized", {})
press_button("[Details]")
assert(vim.api.nvim_win_get_config(0).relative == "editor")
assert(vim.w[vim.api.nvim_get_current_win()].naia_approval_review, "Details button did not expand")
assert_approval_position()
press_button("[Back]")
assert(vim.api.nvim_get_current_win() == source, "Back did not return editing focus")
press_button("[Allow]")
assert(status(request).status == "applied", vim.inspect(status(request)))
assert(vim.api.nvim_buf_get_name(0) == target and vim.api.nvim_win_get_cursor(0)[1] == 3)
assert(not vim.g.injected, "filename interpreted as Ex")
vim.api.nvim_cmd({ cmd = "normal", bang = true, args = { string.char(15) } }, {})
assert(vim.api.nvim_buf_get_name(0) == origin and vim.api.nvim_win_get_cursor(0)[1] == 2,
  "Ctrl-O did not return to the original position")
flush()
request = nav()
flush()
press_button("[Reject]")
assert(status(request).status == "rejected" and vim.api.nvim_buf_get_name(0) == origin)
flush()
request = nav()
flush()
vim.cmd("NaiaReview")
mapping(vim.api.nvim_get_current_buf(), "<Tab>")
mapping(vim.api.nvim_get_current_buf(), "<CR>")
assert(status(request).status == "applied", "keyboard buttons did not allow request")
vim.api.nvim_cmd({ cmd = "normal", bang = true, args = { string.char(15) } }, {})
flush()

request = ui.request("companion", {
  title = "First explanation", content = "# Walkthrough\nSee the [target](source#L3).",
  links = { { label = "target", display_line = 2, path = target, line = 3 } }, reason = "Show notes",
})
flush()
assert(#vim.api.nvim_tabpage_list_wins(0) == 2, "companion appeared before approval")
ui.approve(request.request_id)
assert(status(request).status == "applied", vim.inspect(status(request)))
local first = status(request).result
assert(vim.api.nvim_get_current_win() == source, "companion stole focus")
assert(vim.bo[first.buffer].buftype == "nofile" and not vim.bo[first.buffer].modifiable)
assert(vim.bo[first.buffer].buflisted and not vim.bo[first.buffer].modified)
local original_request, link_request, link_target = ui.request
ui.request = function(kind, args)
  local result = original_request(kind, args)
  if kind == "navigation" then link_request, link_target = result, args end
  return result
end
vim.api.nvim_set_current_win(first.window)
vim.api.nvim_win_set_cursor(0, { 2, 11 })
mapping(first.buffer, "<CR>")
assert(link_request and link_request.status == "pending", "source link bypassed approval")
flush()
ui.approve(link_request.request_id)
assert(status(link_request).status == "applied" and vim.api.nvim_get_current_win() == source)
vim.api.nvim_win_close(first.window, false)
flush()

ui.set_policy("companion", "allow")
local function companion(title, line)
  local result = ui.request("companion", {
    title = title, content = "# " .. title .. "\nSee [target](source).",
    links = { { label = "target", display_line = 2, path = target, line = line } }, reason = "History test",
  })
  flush()
  assert(status(result).status == "applied", vim.inspect(status(result)))
  return status(result).result
end
local second = companion("Second explanation", 2)
local third = companion("Third explanation", 1)
assert(first.buffer ~= second.buffer and second.buffer ~= third.buffer, "companion overwrote a snapshot")
assert(second.window == third.window, "companion accumulated splits")
assert(vim.api.nvim_buf_get_lines(first.buffer, 0, 1, false)[1] == "# Walkthrough")
vim.cmd("NaiaCompanionPrevious")
assert(vim.api.nvim_get_current_buf() == second.buffer)
mapping(second.buffer, "[h")
assert(vim.api.nvim_get_current_buf() == first.buffer)
ui.set_policy("navigation", "off")
vim.api.nvim_win_set_cursor(0, { 2, 11 })
mapping(first.buffer, "<CR>")
assert(link_target.line == 3 and link_request.status == "muted", "old snapshot links changed")
vim.api.nvim_buf_delete(second.buffer, { force = true })
vim.cmd("NaiaCompanionNext")
assert(vim.api.nvim_get_current_buf() == third.buffer, "deleted history was not pruned")
vim.api.nvim_win_close(0, false)
vim.api.nvim_set_current_win(source)
ui.request = original_request
ui.set_policy("navigation", "ask")

vim.fn.setqflist({}, " ", { title = "User list", items = { { filename = origin, lnum = 1, text = "existing" } } })
local old_id = vim.fn.getqflist({ id = 0 }).id
request = ui.request("quickfix", {
  title = "Findings", items = { { path = target, line = 2, text = "finding" } }, reason = "Collect findings",
})
flush()
assert(vim.fn.getqflist({ id = 0 }).id == old_id, "quickfix changed before approval")
ui.approve(request.request_id)
assert(status(request).status == "applied")
assert(vim.fn.getqflist({ id = 0 }).id ~= old_id)
assert(vim.fn.getqflist({ id = old_id, items = 0 }).items[1].text == "existing", "lost quickfix history")
flush()
request = ui.request("quickfix", {
  title = "Append", mode = "append", items = { { path = target, text = "second" } }, reason = "Append finding",
})
vim.fn.setqflist({}, "a", { items = { { filename = origin, lnum = 1, text = "user edit" } } })
ui.approve(request.request_id)
assert(status(request).status == "failed" and #vim.fn.getqflist() == 2, "stale quickfix overwritten")
flush()
request = ui.request("quickfix", {
  title = "Replacement", mode = "replace", open = true,
  items = { { path = target, text = "replacement" } }, reason = "Replace finding",
})
ui.approve(request.request_id)
assert(status(request).status == "applied" and #vim.fn.getqflist() == 1)
assert(vim.api.nvim_get_current_win() == source, "quickfix opening stole focus")
vim.cmd("cclose")
flush()

local function card(index)
  local result = ui.request("card", { title = "Card " .. index, content = "Note " .. index, reason = "Small note" })
  flush()
  assert(status(result).status == "applied", vim.inspect(status(result)))
  return status(result).result
end
for index = 1, 10 do card(index) end
assert(vim.api.nvim_get_current_win() == source, "card stole focus")
local prior = actions.cycle_card(-1)
assert(prior.title == "Card 9")
assert(vim.api.nvim_win_get_config(prior.window).focusable == false)
for _ = 1, 6 do prior = actions.cycle_card(-1) end
assert(prior.title == "Card 3", "card history should retain only eight snapshots")
assert(actions.cycle_card(-1).title == "Card 10", "card history failed to wrap")
vim.cmd("NaiaCardClose")
vim.cmd("NaiaCard")
local card_window = vim.api.nvim_get_current_win()
assert(vim.api.nvim_win_get_config(card_window).relative == "editor", "cannot reopen/focus card")
mapping(vim.api.nvim_get_current_buf(), "[h")
assert(vim.api.nvim_buf_get_lines(0, 0, 1, false)[1] == "Note 9")
ui.set_policy("card", "off")
assert(ui.request("card", {}).status == "muted")
assert(vim.api.nvim_win_get_config(0).relative == "", "muting did not close card")
ui.set_policy("navigation", "off")
assert(nav().status == "muted")
ui.set_policy("navigation", "ask")
request = nav()
ui.set_policy("navigation", "off")
flush()
assert(status(request).status == "muted", "pending request survived mute")
ui.set_policy("navigation", "allow")
request = nav()
ui.set_policy("navigation", "ask")
flush()
assert(status(request).status == "pending", "revoked automatic approval still executed")
ui.reject(request.request_id)
flush()

vim.api.nvim_set_current_win(source)
vim.api.nvim_cmd({ cmd = "edit", args = { origin } }, {})
vim.o.hidden = false
vim.api.nvim_buf_set_lines(0, 0, 1, false, { "unsaved user work" })
request = nav()
ui.approve(request.request_id)
assert(status(request).status == "failed" and vim.api.nvim_buf_get_name(0) == origin, "discarded modified buffer")
assert(vim.api.nvim_buf_get_lines(0, 0, 1, false)[1] == "unsaved user work")
vim.bo.modified = false
flush()

local defer, expire = vim.defer_fn
vim.defer_fn = function(callback) expire = callback end
request = nav()
vim.defer_fn = defer
expire()
flush()
assert(status(request).status == "expired")

local tools = {}
require("naia_ui_tools").register({
  deregister_tool = function(name) tools[name] = nil; return true end,
  register_tool = function(name, spec) tools[name] = spec; return true end,
})
assert(vim.tbl_count(tools) == 5)
ui.set_policy("navigation", "off")
ui.set_policy("companion", "off")
assert(vim.json.decode(tools.jump_to_location.callback({})).status == "muted")
assert(vim.json.decode(tools.show_card.callback({})).status == "muted")
assert(vim.json.decode(tools.show_companion.callback({})).status == "muted")
assert(vim.json.decode(tools.get_ui_request_status.callback({ request_id = request.request_id })).status == "expired")
assert(not pcall(actions.prepare, "companion", {
  title = "Bad", content = "no label", links = { { path = target, label = "absent", display_line = 1 } }, reason = "test",
}))
assert(not pcall(actions.prepare, "card", { title = "Large", content = string.rep("x", 400), reason = "test" }))

-- Surfaces remain distinct even when the theme gives NormalFloat the same
-- background as Normal. Dark/light theme changes must not alter editor colours.
local style = require("naia_ui_style")
for _, palette in ipairs({
  { mode = "dark", background = 0x181818, foreground = 0xeeeeee },
  { mode = "light", background = 0xf5f5f5, foreground = 0x202020 },
}) do
  vim.o.background = palette.mode
  vim.api.nvim_set_hl(0, "Normal", { bg = palette.background, fg = palette.foreground })
  vim.api.nvim_set_hl(0, "NormalFloat", { bg = palette.background, fg = palette.foreground })
  vim.api.nvim_set_hl(0, "DiagnosticInfo", { fg = 0x5599bb })
  vim.api.nvim_set_hl(0, "DiagnosticWarn", { fg = 0xdd9944 })
  vim.api.nvim_exec_autocmds("ColorScheme", {})
  for _, role in ipairs({ "Card", "Approval", "Voice" }) do
    assert(vim.api.nvim_get_hl(0, { name = "NaiaUI" .. role .. "Normal" }).bg ~= palette.background,
      "surface blends into the active buffer")
  end
  assert(vim.api.nvim_get_hl(0, { name = "Normal" }).bg == palette.background)
end
local surface_card = actions.cycle_card(1)
assert(vim.wo[surface_card.window].winblend == 0)
assert(vim.wo[surface_card.window].winhighlight:find("Normal:NaiaUICardNormal", 1, true))
ui.set_policy("navigation", "ask")
request = nav()
flush()
for _, win in ipairs(vim.api.nvim_list_wins()) do
  if vim.w[win].naia_approval then
    assert(vim.wo[win].winblend == 0)
    assert(vim.wo[win].winhighlight:find("Normal:NaiaUIApprovalNormal", 1, true))
  end
end
ui.reject(request.request_id)
flush()
actions.close_card()
local visualizer = require("ai_voice_visualizer")
visualizer.start({ 0.1, waveform = { 0.1, -0.1 } }, 1)
flush()
local found_voice
for _, win in ipairs(vim.api.nvim_list_wins()) do
  if vim.wo[win].winhighlight:find("Normal:NaiaUIVoiceNormal", 1, true) then
    assert(vim.wo[win].winblend == 0)
    found_voice = true
  end
end
assert(found_voice, "voice surface style missing")
assert(vim.api.nvim_get_hl(0, { name = "NAIAVoiceNormal" }).bg
  == vim.api.nvim_get_hl(0, { name = "NaiaUIVoiceNormal" }).bg)
visualizer.stop()
assert(#warnings == 2, "Unexpected warnings: " .. vim.inspect(warnings))
vim.fn.delete(origin)
vim.fn.delete(target)
print("PASS: nonblocking approvals, source links/Ctrl-O, quickfix protection, per-feature policies, companion/card histories, expiry, schemas")
