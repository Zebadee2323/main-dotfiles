-- Run: nvim --headless -u NONE -l tests/ai_tuios_spec.lua
local file = debug.getinfo(1, "S").source:sub(2)
local root = vim.fn.fnamemodify(file, ":p:h:h")
vim.opt.rtp:prepend(root)

local calls, notices, choice, picker, failure
local windows, agents, before_select
local executable = 1
vim.schedule = function(fn) fn() end
vim.notify = function(message) notices[#notices + 1] = message end
vim.fn.executable = function() return executable end
vim.ui.select = function(items, opts, callback)
  picker = items
  assert(opts.format_item(items[1]):find("worker", 1, true))
  if before_select then before_select() end
  callback(choice and items[choice] or nil)
end
vim.system = function(argv, _, callback)
  calls[#calls + 1] = argv
  assert(argv[3] == "--session" and argv[4] == "test-session")
  local data = argv[2] == "list-windows" and { windows = windows }
    or argv[2] == "list-agents" and { agents = agents }
    or { success = true }
  callback(failure or { code = 0, stdout = vim.json.encode(data), stderr = "" })
end

local function reset()
  calls, notices, picker, choice, failure = {}, {}, nil, nil, nil
  executable = 1
  before_select = nil
  vim.env.TUIOS_ENV, vim.env.TUIOS_PANE_ID = "1", "editor"
  vim.env.TUIOS_SESSION = "test-session"
  windows = {
    { window_id = "editor", workspace = 2 },
    { window_id = "worker-1", workspace = 2 },
    { window_id = "worker-2", workspace = 2 },
    { window_id = "elsewhere", workspace = 1 },
  }
  agents = {
    { window_id = "editor", name = "self" },
    { window_id = "elsewhere", name = "wrong workspace" },
    { window_id = "worker-1", name = "worker", state = "idle" },
  }
end
local routing = require("config.ai_tuios")
reset()
vim.env.TUIOS_ENV = nil
assert(not routing.send("hello") and #calls == 0)

reset()
local message = "--literal 'message'\n\n@some file.lua:3-9"
assert(routing.send(message))
assert(#calls == 6 and picker == nil)
assert(calls[5][2] == "send-text")
assert(calls[5][7] == "worker-1" and calls[5][8] == "--")
assert(calls[5][9] == message, "Send raw text with newlines intact and no extra Enter or paste wrappers")
assert(calls[6][2] == "focus-window" and calls[6][6] == "--" and calls[6][7] == "worker-1",
  "Focus the recipient after sending")

reset()
agents[#agents + 1] = { window_id = "worker-2", name = "worker two", state = "idle" }
choice = 2
routing.send(message)
assert(#picker == 2 and calls[5][7] == "worker-2")

reset()
agents[#agents + 1] = { window_id = "worker-2", name = "worker two" }
routing.send(message)
assert(picker and #calls == 2, "Cancelling must not send")

for _, state in ipairs({ "working", "unknown", "needs_input", "none" }) do
  reset()
  agents[3].state = state
  assert(routing.send(message) and #calls == 4 and notices[1]:find(state))
end
reset()
agents[#agents + 1] = { window_id = "worker-2", name = "worker two", state = "idle" }
choice = 1
before_select = function() agents[3].state = "needs_input" end
routing.send(message)
assert(#calls == 4 and notices[1]:find("needs_input"), "Recheck agent state after picking")
reset()
agents[#agents + 1] = { window_id = "worker-2", name = "worker two", state = "idle" }
choice = 1
before_select = function() windows[2].workspace = 3 end
routing.send(message)
assert(#calls == 3 and notices[1]:find("no longer"), "Recheck workspace after picking")

reset()
agents = {}
assert(routing.send(message) and #calls == 2 and #notices == 1)
reset()
windows = {}
assert(routing.send(message) and #calls == 1 and #notices == 1)
reset()
failure = { code = 1, stdout = "", stderr = "forbidden" }
assert(routing.send(message) and #calls == 1 and notices[1]:find("forbidden"))
reset()
failure = { code = 0, stdout = "not json" }
assert(routing.send(message) and #calls == 1 and notices[1]:find("invalid JSON"))
reset()
failure = { code = 0, stdout = '{"success":false,"message":"agents disabled"}' }
assert(routing.send(message) and #calls == 1 and notices[1] == "agents disabled")
reset()
executable = 0
assert(routing.send(message) and #calls == 0 and #notices == 1)
reset()
vim.env.TUIOS_SESSION = nil
assert(routing.send(message) and #calls == 0 and #notices == 1)

-- Commands retain tuios routing without depending on an embedded CLI plugin.
dofile(root .. "/lua/config/ai.lua")
reset()
vim.env.TUIOS_ENV = nil
vim.cmd("AISend explain this")
assert(#calls == 0 and notices[1]:find("AICopy", 1, true))
reset()
vim.cmd("AISend explain this")
assert(calls[5][2] == "send-text")
assert(calls[5][9]:find("explain this\n\n@", 1, true))
reset()
vim.cmd("AIMessage hello there")
assert(calls[5][9] == "hello there")
assert(calls[6][2] == "focus-window")
reset()
vim.env.TUIOS_ENV = nil
vim.cmd("AIMessage hello there")
assert(#calls == 0 and notices[1]:find("AICopy", 1, true))
reset()
agents[3].state = "needs_input"
vim.cmd("AIMessage hello there")
assert(#calls == 4, "Blocked tuios sends must not use another destination")
assert(vim.api.nvim_get_commands({}).AI == nil, "Sidekick toggle command must be removed")
reset()
vim.cmd("AICopy explain this")
assert(vim.fn.getreg('"'):find("explain this\n\n@", 1, true))
print("PASS: AI commands retain tuios routing and clipboard support without Sidekick")
