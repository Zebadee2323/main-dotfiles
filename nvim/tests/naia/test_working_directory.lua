local helpers = dofile("tests/naia/helpers.lua")
local naia = require("naia")
local state = require("naia.state")
local mcp = require("mcp-tools")

state.reset()
naia.setup()

local tool = state.tools.get_working_directory
helpers.truthy(tool, "setup should register the working directory tool")
helpers.eq(next(tool.args), nil, "the tool should have no arguments")

local _, err = naia.setup()
helpers.eq(err, nil, "repeated setup should not fail on built-in registration")
helpers.eq(state.tools.get_working_directory, tool, "repeated setup should preserve the registered tool")
helpers.eq(naia.status().tool_count, 1, "repeated setup should not duplicate tools")

local function working_directory()
  local result
  mcp.list_tools().get_working_directory.execute(function(value) result = value end, {})
  return result
end

local root = vim.fn.getcwd()
helpers.eq(working_directory(), root, "the tool should return Neovim's working directory")

vim.cmd("cd " .. vim.fn.fnameescape(root .. "/tests"))
helpers.eq(working_directory(), root .. "/tests", "the tool should read directory changes on each call")

vim.cmd("tcd " .. vim.fn.fnameescape(root .. "/lua"))
helpers.eq(working_directory(), root .. "/lua", "the tool should respect tab-local directories")

vim.cmd("lcd " .. vim.fn.fnameescape(root .. "/plugin"))
helpers.eq(working_directory(), root .. "/plugin", "the tool should respect window-local directories")

vim.cmd("cd " .. vim.fn.fnameescape(root))
helpers.eq(working_directory(), root, "the tool should follow a return to the global directory")

vim.cmd("qa!")
