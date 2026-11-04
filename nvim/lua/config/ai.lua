-- Agent messaging lives in tuios; no embedded CLI plugin is required.
local function location(opts)
  local path = vim.api.nvim_buf_get_name(0)
  if path == "" then
    path = "[No Name]"
  else
    local cwd = vim.fn.getcwd()
    local result = vim.fn.systemlist({ "git", "-C", cwd, "rev-parse", "--show-toplevel" })
    local root = vim.v.shell_error == 0 and result[1] or cwd
    local relative = vim.fs.relpath(root, path)
    if relative and relative ~= ".." and not relative:match("^%.%.[/\\]") then
      path = relative
    end
  end
  if opts.range and opts.line1 and opts.line2 then
    local lines = opts.line1 == opts.line2 and tostring(opts.line1)
      or string.format("%d-%d", opts.line1, opts.line2)
    return string.format("@%s:%s", path, lines)
  end
  return string.format("@%s:%d", path, vim.api.nvim_win_get_cursor(0)[1])
end

local function message(opts)
  local reference = location(opts)
  local prompt = vim.trim(opts.args or "")
  return prompt == "" and reference or (prompt .. "\n\n" .. reference), reference
end

local function restore_selection(opts)
  if opts.range and opts.line1 and opts.line2 and opts.line2 >= opts.line1 then
    vim.schedule(function() pcall(vim.cmd, "normal! gv") end)
  end
end

local function send(text)
  if not require("config.ai_tuios").send(text) then
    vim.notify("Launch an agent in tuios to send messages; use :AICopy outside tuios", vim.log.levels.WARN)
  end
end

vim.api.nvim_create_user_command("AIMessage", function(opts)
  if vim.trim(opts.args) == "" then
    vim.notify("AIMessage requires a message", vim.log.levels.ERROR)
    return
  end
  send(opts.args)
end, { nargs = "+", desc = "Insert raw text into a tuios workspace agent" })

vim.api.nvim_create_user_command("AISend", function(opts)
  local text = message(opts)
  send(text)
  restore_selection(opts)
end, { nargs = "*", range = true, desc = "Insert file/line context into a tuios workspace agent" })

for name, prompt in pairs({
  AICommit = "Git commit staged changes only, decide a commit message based only on those changes",
  AICommitSkill = "Run the git-commit-staged skill.",
  AICommitSkillAuto = "Run the git-commit-staged skill, automated.",
}) do
  vim.api.nvim_create_user_command(name, function() send(prompt) end,
    { desc = "Send staged commit prompt to a tuios agent" })
end

vim.api.nvim_create_user_command("AICopy", function(opts)
  local text, reference = message(opts)
  vim.fn.setreg("+", text)
  vim.fn.setreg('"', text)
  vim.notify("Copied to clipboard:\n" .. reference)
  restore_selection(opts)
end, { nargs = "*", range = true, desc = "Copy file/line context to the clipboard" })
