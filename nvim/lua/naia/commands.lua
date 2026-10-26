local M = {}
local initialized = false

local function recreate_user_command(name, rhs, opts)
  pcall(vim.api.nvim_del_user_command, name)
  vim.api.nvim_create_user_command(name, rhs, opts)
end

local function command_result(ok, err, success_message)
  if ok then
    if success_message then
      vim.notify(success_message, vim.log.levels.INFO, { title = "Naia" })
    end
    return
  end

  vim.notify(err or "Naia command failed", vim.log.levels.ERROR, { title = "Naia" })
end

local function show_status_buffer(status)
  vim.cmd("botright new")

  local buf = vim.api.nvim_get_current_buf()
  local lines = vim.split(vim.inspect(status), "\n", { plain = true })

  vim.bo[buf].buftype = "nofile"
  vim.bo[buf].bufhidden = "wipe"
  vim.bo[buf].swapfile = false
  vim.bo[buf].modifiable = true

  vim.api.nvim_buf_set_lines(buf, 0, -1, false, lines)
  vim.bo[buf].modifiable = false

  vim.keymap.set("n", "q", "<cmd>close<cr>", {
    buffer = buf,
    silent = true,
    nowait = true,
  })

  vim.api.nvim_win_set_cursor(0, { 1, 0 })
end

function M.setup()
  if initialized then return end
  initialized = true

  recreate_user_command("NaiaStart", function()
    local ok, err = require("naia").start()
    command_result(ok, err, "Naia tools attached")
  end, { desc = "Attach Naia tools to mcp-tools (does not start the bridge)" })

  recreate_user_command("NaiaStop", function()
    local ok, err = require("naia").stop()
    command_result(ok, err, "Naia tools detached")
  end, { desc = "Detach Naia tools without stopping mcp-tools" })

  recreate_user_command("NaiaRestart", function()
    local ok, err = require("naia").restart()
    command_result(ok, err, "Naia tools re-registered")
  end, { desc = "Re-register Naia tools without restarting mcp-tools" })

  recreate_user_command("NaiaStatus", function()
    show_status_buffer(require("naia").status())
  end, { desc = "Show Naia registration and shared bridge status" })
end

return M
