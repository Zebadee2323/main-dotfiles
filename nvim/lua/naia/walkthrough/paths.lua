-- Live source lookup for trusted local tours. Portable-file containment is
-- deliberately stricter and stays in file.lua.
local M = {}

function M.normalize(path)
  path = vim.fn.fnamemodify(path, ":p")
  return vim.fs.normalize(vim.uv.fs_realpath(path) or path)
end

function M.git_root(path)
  local args = { "git" }
  if path then
    local absolute = vim.fn.fnamemodify(path, ":p")
    local dir = vim.fn.isdirectory(absolute) == 1 and absolute or vim.fn.fnamemodify(absolute, ":h")
    vim.list_extend(args, { "-C", dir })
  end
  vim.list_extend(args, { "rev-parse", "--show-toplevel" })
  local ok, lines = pcall(vim.fn.systemlist, args)
  if ok and vim.v.shell_error == 0 and lines[1] and lines[1] ~= "" then return lines[1] end
end

function M.resolve(path)
  if type(path) ~= "string" or vim.trim(path) == "" then return nil end
  path = vim.trim(path)
  if path:sub(1, 1) == "/" or path:sub(1, 1) == "~" or path:match("^%a:[/\\]") then
    local absolute = M.normalize(path)
    return vim.fn.filereadable(absolute) == 1 and absolute or nil
  end
  local roots, seen = {}, {}
  local function add(root)
    if not root then return end
    root = M.normalize(root)
    if not seen[root] then roots[#roots + 1], seen[root] = root, true end
  end
  local cwd = vim.fn.getcwd()
  add(cwd)
  add(M.git_root(cwd))
  local bufname = vim.api.nvim_buf_get_name(0)
  if bufname ~= "" then
    add(vim.fn.fnamemodify(bufname, ":p:h"))
    add(M.git_root(bufname))
  end
  for _, root in ipairs(roots) do
    local candidate = M.normalize(root .. "/" .. path)
    if vim.fn.filereadable(candidate) == 1 then return candidate end
  end
end

return M
