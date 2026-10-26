-- Run: nvim --headless -u NONE -l tests/naia_ui_policy_spec.lua
local file = debug.getinfo(1, "S").source:sub(2)
vim.opt.rtp:prepend(vim.fn.fnamemodify(file, ":p:h:h"))
-- An omitted capability must retain auto-allow while explicit ask/off still win.
vim.g.naia_ui_policies = { quickfix = "ask", navigation = "off", card = "off" }
local ui = require("naia.ui")
assert(ui.request("navigation", {}).status == "muted", "navigation should auto-reject")
assert(ui.request("card", {}).status == "muted", "cards should auto-reject")
local request = ui.request("companion", { title = "Auto-approved", content = "A note", reason = "Test startup policy" })
assert(vim.wait(1000, function() return ui.status(request.request_id).status ~= "pending" end, 10))
assert(ui.status(request.request_id).status == "applied", "companion should auto-approve")
local pending = ui.request("quickfix", { title = "Still asks", items = {}, reason = "Test independent policy" })
assert(ui.status(pending.request_id).status == "pending")
ui.reject(pending.request_id)
ui.set_policy("companion", "off")
assert(ui.request("companion", {}).status == "muted")
assert(vim.g.naia_ui_policies.companion == nil and vim.g.naia_ui_policies.quickfix == "ask",
  "defaults/session policy modified user configuration")
print("PASS: independent startup auto-approve, auto-reject, ask, and session overrides")
