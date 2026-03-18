local function copy_to_clipboard(label, value)
  vim.fn.setreg("+", value)
  print(label .. ": " .. value)
end

local function trim_token(token)
  return token
    :gsub("^[\"'`%(%[%{<]+", "")
    :gsub("[\"'`,;:%)%]%}>]+$", "")
end

local function resolve_existing_path(path, base_dir)
  local candidates = {}

  if path:sub(1, 1) == "/" then
    candidates = { path }
  elseif path:sub(1, 2) == "~/" then
    candidates = { vim.fn.expand(path) }
  else
    candidates = {
      vim.fs.normalize(vim.fs.joinpath(base_dir, path)),
      vim.fs.normalize(vim.fs.joinpath(vim.loop.cwd(), path)),
    }
  end

  for _, candidate in ipairs(candidates) do
    if vim.fn.filereadable(candidate) == 1 then
      return candidate
    end
  end
end

local function token_to_qf_item(token, base_dir, source_file, source_lnum)
  local cleaned = trim_token(token)
  if cleaned == "" or not cleaned:find("/") then
    return nil
  end

  local path = cleaned
  local lnum = 1
  local maybe_path, maybe_lnum = cleaned:match("^(.-):(%d+)$")
  if maybe_path and maybe_path ~= "" then
    path = maybe_path
    lnum = tonumber(maybe_lnum)
  end

  local resolved = resolve_existing_path(path, base_dir)
  if not resolved then
    return nil
  end

  return {
    filename = resolved,
    lnum = lnum,
    text = string.format("Found in %s:%d", source_file, source_lnum),
  }
end

local function paths_to_quickfix(opts)
  local input_file = vim.fn.fnamemodify(vim.fn.expand(opts.args), ":p")
  if vim.fn.filereadable(input_file) ~= 1 then
    vim.notify("File not found: " .. input_file, vim.log.levels.ERROR)
    return
  end

  local lines = vim.fn.readfile(input_file)
  local base_dir = vim.fn.fnamemodify(input_file, ":h")
  local items = {}

  for source_lnum, line in ipairs(lines) do
    for token in line:gmatch("%S+") do
      local item = token_to_qf_item(token, base_dir, input_file, source_lnum)
      if item then
        items[#items + 1] = item
      end
    end
  end

  if #items == 0 then
    vim.notify("No file paths found in " .. input_file, vim.log.levels.WARN)
    return
  end

  vim.fn.setqflist({}, " ", {
    title = "Paths from " .. vim.fn.fnamemodify(input_file, ":~:.") ,
    items = items,
  })
  vim.cmd("copen")
  vim.notify(string.format("Added %d path(s) to quickfix", #items))
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

vim.api.nvim_create_user_command("PathsToQuickFix", paths_to_quickfix, {
  nargs = 1,
  complete = "file",
})
