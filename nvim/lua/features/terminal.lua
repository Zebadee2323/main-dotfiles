local M = {}
local initialized = false

local function open_terminal(split, args)
  vim.cmd(split .. " | terminal " .. (args or ""))
  vim.opt_local.number = true
  vim.opt_local.relativenumber = true
  vim.cmd("startinsert")
end

function M.setup()
  if initialized then return end
  initialized = true
  local opts = { nargs = "*", complete = "shellcmd" }
  vim.api.nvim_create_user_command("T", function(args)
    open_terminal("vsplit", args.args)
  end, opts)
  vim.api.nvim_create_user_command("Ts", function(args)
    open_terminal("split", args.args)
  end, opts)
  -- Span the full width below all existing windows.
  vim.api.nvim_create_user_command("Tb", function(args)
    open_terminal("botright 10split", args.args)
  end, opts)
end

return M
