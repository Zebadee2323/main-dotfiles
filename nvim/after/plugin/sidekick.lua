require("sidekick").setup({
  nes = {
    enabled = false,
  },
})

vim.keymap.set('n', '<c-p><c-g>', ':Sidekick cli toggle<CR>')

-- Get git root if available
local function get_git_root(path)
  local dir = path and vim.fn.fnamemodify(path, ":p:h") or nil
  local cmd = dir and ("git -C " .. vim.fn.shellescape(dir) .. " rev-parse --show-toplevel")
    or "git rev-parse --show-toplevel"

  local ok, lines = pcall(vim.fn.systemlist, cmd)
  if not ok or type(lines) ~= "table" or #lines == 0 then
    return nil
  end

  local root = lines[1]
  if root == nil or root == "" then
    return nil
  end

  return root
end

-- Return path relative to git repo if possible
local function get_repo_relative_path()
  local bufname = vim.api.nvim_buf_get_name(0)
  if bufname == "" then
    return "[No Name]"
  end

  local abs_buf = vim.fn.fnamemodify(bufname, ":p")
  local git_root = get_git_root(abs_buf)

  if git_root then
    local abs_root = vim.fn.fnamemodify(git_root, ":p"):gsub("[/\\]$", "")

    local rel = vim.fs.relpath(abs_root, abs_buf)
    if rel then
      if rel == "" then
        return "."
      end
      return rel
    end
  end

  -- fallback: path relative to cwd
  local cwd_rel = vim.fn.fnamemodify(bufname, ":.")
  if cwd_rel ~= "" then
    return cwd_rel
  end

  return bufname
end

-- Restore visual selection after running command
local function maybe_restore_visual(opts)
  if opts.range and opts.line1 and opts.line2 and opts.line2 >= opts.line1 then
    vim.schedule(function()
      pcall(vim.cmd, "normal! gv")
    end)
  end
end

-- Send metadata: file + line or line range (line info after a space)
vim.api.nvim_create_user_command("AISend", function(opts)
  local prefix = vim.trim(opts.args or "")
  local path = get_repo_relative_path()

  local body
  if opts.range and opts.line1 and opts.line2 and opts.line2 >= opts.line1 then
    if opts.line1 == opts.line2 then
      body = string.format("@%s %d", path, opts.line1)
    else
      body = string.format("@%s %d-%d", path, opts.line1, opts.line2)
    end
  else
    local line = vim.api.nvim_win_get_cursor(0)[1]
    body = string.format("@%s %d", path, line)
  end

  local msg = prefix ~= "" and (prefix .. "\n\n" .. body) or body

  pcall(require("sidekick.cli").send, {
    msg = msg,
    submit = true,
  })

  maybe_restore_visual(opts)
end, {
  nargs = "*",
  range = true,
  desc = "Send file line or file start-end to Sidekick",
})

-- Copy metadata: file + line or line range to clipboard (line info after a space)
vim.api.nvim_create_user_command("AICopy", function(opts)
  local prefix = vim.trim(opts.args or "")
  local path = get_repo_relative_path()

  local body
  if opts.range and opts.line1 and opts.line2 and opts.line2 >= opts.line1 then
    if opts.line1 == opts.line2 then
      body = string.format("@%s %d", path, opts.line1)
    else
      body = string.format("@%s %d-%d", path, opts.line1, opts.line2)
    end
  else
    local line = vim.api.nvim_win_get_cursor(0)[1]
    body = string.format("@%s %d", path, line)
  end

  local msg = prefix ~= "" and (prefix .. "\n\n" .. body) or body

  -- copy to OS clipboard
  vim.fn.setreg("+", msg)
  vim.fn.setreg('"', msg)

  vim.notify("Copied to clipboard:\n" .. body)

  maybe_restore_visual(opts)
end, {
  nargs = "*",
  range = true,
  desc = "Copy file line or file start-end to clipboard",
})
