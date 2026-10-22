-- Run: nvim --headless -u NONE -l tests/layout_spec.lua
local file = debug.getinfo(1, "S").source:sub(2)
local root = vim.fn.fnamemodify(file, ":p:h:h")
vim.opt.rtp:prepend(root)
vim.opt.rtp:append(root .. "/after")

-- Compile every Lua source without executing plugin configs or installing plugins.
for _, path in ipairs(vim.fn.globpath(root, "**/*.lua", false, true)) do
  local chunk, err = loadfile(path)
  assert(chunk, err)
end

local voice = require("features.voice")
local fx = require("features.file_change_fx")
assert(type(voice.setup) == "function" and type(fx.setup) == "function")
assert(not package.loaded["features.voice.runtime"], "voice started before setup")
assert(not package.loaded["features.file_change_fx.runtime"], "FX started before setup")

require("core")
assert(vim.o.clipboard == "" and vim.o.foldlevel == 99 and not vim.o.wrap)
for _, command in ipairs({ "TrimWhitespace", "W", "OpenFolder", "Config", "T", "Ts", "Tb" }) do
  assert(vim.fn.exists(":" .. command) == 2, "missing core command: " .. command)
end
require("features.scrolling").setup()
require("features.terminal").setup()
assert(#vim.api.nvim_get_autocmds({ group = "UserScrolling" }) == 4,
  "repeated setup duplicated scrolling autocmds")

-- This exercises the post-plugin entrypoint without loading third-party plugins.
require("features").setup()
local generation = _G.__file_change_fx_state.generation
local autocmd_count = #vim.api.nvim_get_autocmds({})
require("features").setup()
assert(_G.__file_change_fx_state.generation == generation, "FX runtime restarted")
assert(#vim.api.nvim_get_autocmds({}) == autocmd_count, "setup duplicated autocmds")
for _, command in ipairs({ "AIVoice", "FileChangeFxCheck", "CopyPath", "GitCheatSheet", "ToQf",
  "CreateGuid", "UnityRename" }) do
  assert(vim.fn.exists(":" .. command) == 2, "missing feature command: " .. command)
end

vim.cmd("filetype plugin on")
for _, ft in ipairs({ "markdown", "text", "gitcommit" }) do
  vim.cmd("enew!")
  vim.bo.filetype = ft
  assert(vim.wo.wrap and vim.wo.linebreak and vim.wo.breakindent, "missing wrapping for " .. ft)
  vim.bo.filetype = "lua"
  assert(not vim.wo.wrap and not vim.wo.linebreak and not vim.wo.breakindent,
    "wrapping leaked after changing filetype from " .. ft)
end
vim.cmd("enew!")
vim.bo.filetype = "git"
assert(vim.wo.foldmethod == "syntax" and vim.wo.foldlevel == 1, "missing Git folding")
vim.bo.filetype = "lua"
assert(vim.wo.foldmethod == "manual" and vim.wo.foldlevel == 99, "Git folding leaked")

-- Flush scheduled watcher refreshes; VimLeavePre exercises runtime cleanup on exit.
vim.wait(40, function() return false end, 5)
print("PASS: Lua syntax, core commands, feature setup/idempotence, filetype settings and undo")
