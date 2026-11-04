local source = debug.getinfo(1, "S").source:sub(2)
local root = vim.fn.fnamemodify(source, ":p:h:h:h")
local xdg_root = vim.fn.tempname()

vim.fn.mkdir(xdg_root .. "/state", "p")
vim.fn.mkdir(xdg_root .. "/cache", "p")
vim.fn.mkdir(xdg_root .. "/data", "p")
vim.fn.mkdir(xdg_root .. "/config", "p")

vim.env.XDG_STATE_HOME = xdg_root .. "/state"
vim.env.XDG_CACHE_HOME = xdg_root .. "/cache"
vim.env.XDG_DATA_HOME = xdg_root .. "/data"
vim.env.XDG_CONFIG_HOME = xdg_root .. "/config"

vim.go.loadplugins = false
vim.opt.shadafile = "NONE"
vim.opt.runtimepath = root .. "," .. vim.env.VIMRUNTIME
vim.opt.packpath = root

vim.notify = function(...)
  return ...
end

package.path = table.concat({
  root .. "/lua/?.lua",
  root .. "/lua/?/init.lua",
  package.path,
}, ";")

if not vim.env.MCP_TOOLS_PATH then
  package.loaded["mcp-tools"] = dofile(root .. "/tests/naia/fixtures/mcp_tools.lua")
end

require("naia.commands").setup()
vim.api.nvim_create_autocmd("VimLeavePre", { callback = function() vim.fn.delete(xdg_root, "rf") end })
