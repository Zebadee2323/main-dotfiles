local tools = {}
local M = {}
local function forbidden() error("Naia must never configure or control the mcp-tools bridge") end
M.setup, M.start, M.stop = forbidden, forbidden, forbidden
M.list_tools = function() return tools end
M.register = function(tool)
  tools[tool.name] = { name = tool.name, description = tool.description, args = tool.args, execute = tool.execute }
end
M.unregister = function(name) tools[name] = nil end
return M
