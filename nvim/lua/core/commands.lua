vim.api.nvim_create_user_command("TrimWhitespace", function()
  local view = vim.fn.winsaveview()
  vim.cmd([[%s/\s\+$//e]])
  vim.fn.winrestview(view)
  vim.notify("Trailing whitespace trimmed", vim.log.levels.INFO)
end, { desc = "Trim trailing whitespace in current buffer" })

vim.api.nvim_create_user_command("W", "w", { desc = "Write current buffer" })

vim.api.nvim_create_user_command("OpenCurrentFolder", function()
  local dir = vim.fn.expand("%:p:h")

  if vim.fn.has("mac") == 1 then
    vim.fn.jobstart({ "open", dir }, { detach = true })
  elseif vim.fn.has("win32") == 1 then
    vim.fn.jobstart({ "explorer", dir }, { detach = true })
  else
    vim.fn.jobstart({ "xdg-open", dir }, { detach = true })
  end
end, { desc = "Open current file folder in system file manager" })

vim.api.nvim_create_user_command("OpenFolder", function(opts)
  local path = vim.fn.expand(opts.args)
  if path == "" then
    vim.notify("OpenFolder: a path is required", vim.log.levels.ERROR)
    return
  end

  -- Resolve a file to its parent directory.
  local dir = (vim.fn.isdirectory(path) == 1) and path or vim.fn.fnamemodify(path, ":h")
  if vim.fn.isdirectory(dir) ~= 1 then
    vim.notify("OpenFolder: no such directory: " .. dir, vim.log.levels.ERROR)
    return
  end

  if vim.fn.has("mac") == 1 then
    vim.fn.jobstart({ "open", dir }, { detach = true })
  elseif vim.fn.has("win32") == 1 then
    vim.fn.jobstart({ "explorer", dir }, { detach = true })
  else
    vim.fn.jobstart({ "xdg-open", dir }, { detach = true })
  end
end, {
  nargs = 1,
  complete = "file",
  desc = "Open the given folder (or a file's folder) in the system file manager",
})

vim.api.nvim_create_user_command("OpenInBrowser", function()
  local file = vim.fn.expand("%:p")
  if file == "" then
    vim.notify("OpenInBrowser: current buffer has no file", vim.log.levels.ERROR)
    return
  end

  local url = "file://" .. file:gsub("[^%w%-%._~/]", function(c)
    return string.format("%%%02X", string.byte(c))
  end)

  if vim.fn.has("mac") == 1 then
    vim.fn.jobstart({ "open", url }, { detach = true })
  elseif vim.fn.has("win32") == 1 then
    vim.fn.jobstart({ "cmd", "/c", "start", "", url }, { detach = true })
  else
    vim.fn.jobstart({ "xdg-open", url }, { detach = true })
  end
end, { desc = "Open current file in the default web browser" })

vim.api.nvim_create_user_command("Config", ":Oil " .. vim.fn.stdpath("config"), {})
vim.api.nvim_create_user_command("ConfigSafe", ":Vexplore " .. vim.fn.stdpath("config"), {})
