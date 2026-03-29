local sidekick_naia_wrapper = require("naia").wrapper_path()

require("sidekick").setup({
  nes = {
    enabled = false,
  },
  cli = {
    tools = {
      opencode_naia = {
        cmd = { sidekick_naia_wrapper, "opencode" },
        is_proc = "\\<opencode\\>",
        continue = { "--continue" },
        native_scroll = true,
        url = "https://github.com/sst/opencode",
      },
      codex_naia = {
        cmd = { sidekick_naia_wrapper, "codex" },
        is_proc = "\\<codex\\>",
        resume = { "resume" },
        continue = { "resume", "--last" },
        url = "https://github.com/openai/codex",
      },
    },
  },
})

vim.keymap.set('n', '<c-p><c-g>', ':Sidekick cli toggle<CR>')

vim.api.nvim_create_user_command("AI", function()
  require("sidekick.cli").toggle("codex_naia")
end, {
  desc = "Open Sidekick with the codex_naia tool",
})

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

local function build_ai_location_reference(opts)
  opts = opts or {}

  local separator = opts.separator or ":"
  local path = get_repo_relative_path()

  if opts.range and opts.line1 and opts.line2 and opts.line2 >= opts.line1 then
    if opts.line1 == opts.line2 then
      return string.format("@%s%s%d", path, separator, opts.line1)
    end

    return string.format("@%s%s%d-%d", path, separator, opts.line1, opts.line2)
  end

  local line = opts.current_line or vim.api.nvim_win_get_cursor(0)[1]
  return string.format("@%s%s%d", path, separator, line)
end

local function build_ai_message(opts)
  opts = opts or {}

  local prefix = vim.trim(opts.args or "")
  local body = build_ai_location_reference(opts)

  if prefix ~= "" then
    return prefix .. "\n\n" .. body, body
  end

  return body, body
end

local function send_raw_message_to_sidekick(msg)
  local trimmed = vim.trim(msg or "")
  if trimmed == "" then
    vim.notify("AIMessage requires a message", vim.log.levels.ERROR)
    return false
  end

  pcall(require("sidekick.cli").send, {
    msg = msg,
    submit = true,
  })

  return true
end

vim.api.nvim_create_user_command("AIMessage", function(opts)
  send_raw_message_to_sidekick(opts.args or "")
end, {
  nargs = "+",
  desc = "Send a raw message to Sidekick",
})

-- Send metadata: file + line or line range (line info after a space)
vim.api.nvim_create_user_command("AISend", function(opts)
  local msg = build_ai_message(vim.tbl_extend("force", opts, {
    separator = ":",
  }))

  vim.api.nvim_cmd({
    cmd = "AIMessage",
    args = { msg },
  }, {})

  maybe_restore_visual(opts)
end, {
  nargs = "*",
  range = true,
  desc = "Send file line or file start-end to Sidekick",
})

vim.api.nvim_create_user_command("AICommit", function()
  vim.api.nvim_cmd({
    cmd = "AIMessage",
    args = { "exec `git commit -m`, you decide the message." },
  }, {})
end, {
  desc = "Send git commit staged prompt to Sidekick",
})

-- Copy metadata: file + line or line range to clipboard (line info after a space)
vim.api.nvim_create_user_command("AICopy", function(opts)
  local msg, body = build_ai_message(vim.tbl_extend("force", opts, {
    separator = ":",
  }))

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
