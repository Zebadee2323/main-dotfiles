local files = require("naia.walkthrough.file")
local uv = vim.uv or vim.loop
local base = vim.fn.tempname()
local first, second = base .. "/alice", base .. "/bob"
for _, root in ipairs({ first, second }) do
  vim.fn.mkdir(root .. "/src", "p")
  vim.fn.writefile({ "one", "two" }, root .. "/src/main.lua")
end
first, second = uv.fs_realpath(first), uv.fs_realpath(second)
local function rejects(fn)
  local ok, err = pcall(fn)
  assert(not ok, "Expected rejection: " .. tostring(err))
end
local loc = { path = first .. "/src/main.lua", resolved_path = first .. "/src/main.lua",
  line_start = 1, line_end = 2, description = "Tour", id = 1, _buffer = 99,
  private_metadata = first .. "/private" }
loc.beats = { { path = loc.path, description = "Beat", line_start = 1, line_end = 1 } }
loc.highlights = { { path = loc.path, line_start = 1, line_end = 1, label = "Entry" } }
loc.comparison = {
  left = { path = first .. "/src/deleted.lua", line_start = 1, line_end = 1, text = "before" },
  right = { path = loc.path, line_start = 1, line_end = 2 }, diff = true,
}
local text = files.encode({ steps = { loc } }, first)
assert(not text:find(base, 1, true) and not text:find("private_metadata", 1, true))
local loaded = files.decode(text, second)[1]
assert(loaded.path == second .. "/src/main.lua")
assert(loaded.beats[1].path == loaded.path and loaded.highlights[1].path == loaded.path)
assert(loaded.comparison.left.path == second .. "/src/deleted.lua" and loaded.comparison.left.text == "before")
local portable = vim.json.decode(text)
local function invalid_path(path)
  local copy = vim.deepcopy(portable)
  copy.steps[1].comparison.left.path = path
  rejects(function() files.decode(vim.json.encode(copy), second) end)
end
for _, path in ipairs({ "/etc/passwd", "../alice/src/main.lua", "src/../../secret", "~/secret",
  "C:/Users/private", "src\\main.lua", "./src/main.lua" }) do invalid_path(path) end
assert(uv.fs_symlink(first, second .. "/escape"))
invalid_path("escape/src/main.lua")
invalid_path("escape/src/missing.lua")
local copy = vim.deepcopy(portable)
copy.steps[1].resolved_path = "/private"
rejects(function() files.decode(vim.json.encode(copy), second) end)
copy = vim.deepcopy(portable)
copy.version = 99
rejects(function() files.decode(vim.json.encode(copy), second) end)
loc.path, loc.resolved_path = "/etc/passwd", "/etc/passwd"
rejects(function() files.encode({ steps = { loc } }, first) end)
vim.fn.delete(base, "rf")
print("portable walkthrough file tests passed")
vim.cmd("qa!")
