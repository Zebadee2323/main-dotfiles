local function copy_to_clipboard(label, value)
  vim.fn.setreg("+", value)
  print(label .. ": " .. value)
end

-- Full absolute path
vim.api.nvim_create_user_command("CopyPath", function()
  copy_to_clipboard("Copied path", vim.fn.expand("%:p"))
end, {})

-- Directory of current file
vim.api.nvim_create_user_command("CopyDir", function()
  copy_to_clipboard("Copied dir", vim.fn.expand("%:p:h"))
end, {})

-- Relative path (from cwd)
vim.api.nvim_create_user_command("CopyRelPath", function()
  copy_to_clipboard("Copied relative path", vim.fn.expand("%"))
end, {})

-- file:// URL
vim.api.nvim_create_user_command("CopyFileUrl", function()
  copy_to_clipboard(
    "Copied file URL",
    "file://" .. vim.fn.expand("%:p")
  )
end, {})

-- Path relative to git root (falls back to relative path)
vim.api.nvim_create_user_command("CopyGitPath", function()
  local git_root = vim.fn.systemlist("git rev-parse --show-toplevel")[1]
  if vim.v.shell_error ~= 0 or not git_root then
    copy_to_clipboard("Copied relative path", vim.fn.expand("%"))
    return
  end

  local full_path = vim.fn.expand("%:p")
  local git_path = full_path:gsub("^" .. vim.pesc(git_root) .. "/", "")
  copy_to_clipboard("Copied git path", git_path)
end, {})
