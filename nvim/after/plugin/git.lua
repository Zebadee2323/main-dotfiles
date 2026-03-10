local M = {}

-- Try to find the git repo root (prefer Fugitive, fallback to git)
local function get_git_root()
  -- Fugitive (if available)
  local ok, gitdir = pcall(vim.fn.FugitiveGitDir)
  if ok and gitdir and gitdir ~= '' then
    return vim.fn.fnamemodify(gitdir, ':h')
  end

  -- Fallback: `git rev-parse`
  local out = vim.fn.systemlist({ 'git', 'rev-parse', '--show-toplevel' })
  if vim.v.shell_error == 0 and out[1] and out[1] ~= '' then
    return out[1]
  end

  return nil
end

-- Extract all paths from a line.
-- Handles:
--   git diff --name-only           -> "path"
--   git diff --name-status         -> "M\tpath"
--   ... with renames               -> "R100\told\tnew"
local function extract_paths(line)
  line = vim.trim(line)
  if line == '' then
    return {}
  end

  -- Prefer splitting on TAB because git uses TAB as a field separator.
  local fields = vim.split(line, '\t', { plain = true })

  -- If no tabs, just treat the whole line as a single "path-ish" field.
  if #fields == 1 then
    return { fields[1] }
  end

  -- name-status: first field is the status (M, A, D, R100, etc.)
  local start_idx = 1
  local status = fields[1]
  if status:match('^[ACDMRTUXB?!][0-9]*$') then
    start_idx = 2
  end

  local paths = {}
  for i = start_idx, #fields do
    local p = fields[i]
    if p ~= '' then
      table.insert(paths, p)
    end
  end

  return paths
end

local function make_abs(root, path)
  -- Already absolute (unix or Windows drive)
  if path:match('^/') or path:match('^%a:[/\\]') then
    return vim.fs.normalize(path)
  end

  if root and root ~= '' then
    return vim.fs.normalize(vim.fs.joinpath(root, path))
  end

  -- Fallback: relative to current working dir
  return vim.fs.normalize(vim.fs.joinpath(vim.loop.cwd(), path))
end

function M.buf_paths_to_qf()
  local bufnr = vim.api.nvim_get_current_buf()
  local lines = vim.api.nvim_buf_get_lines(bufnr, 0, -1, false)
  local git_root = get_git_root()

  local seen = {}   -- abs_path -> true
  local paths = {}  -- list of abs paths

  for _, line in ipairs(lines) do
    for _, rel in ipairs(extract_paths(line)) do
      local abs = make_abs(git_root, rel)
      if not seen[abs] then
        seen[abs] = true
        table.insert(paths, abs)
      end
    end
  end

  table.sort(paths)

  local items = {}
  for _, abs in ipairs(paths) do
    table.insert(items, {
      filename = abs,
      lnum = 1,
      col = 1,
      text = abs,  -- you could also use vim.fs.basename(abs)
    })
  end

  vim.fn.setqflist({}, ' ', {
    title = 'git paths from buffer',
    items = items,
  })

  vim.cmd('copen')
end

vim.api.nvim_create_user_command('ToQf', function()
  M.buf_paths_to_qf()
end, {})

return M
