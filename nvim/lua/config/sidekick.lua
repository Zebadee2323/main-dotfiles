local sidekick_naia_wrapper = require("naia").wrapper_path()

require("sidekick").setup({
  nes = {
    enabled = false,
  },
  cli = {
    win = {
      -- Let the CLI handle Ctrl-P instead of opening Sidekick's prompt picker.
      keys = { prompt = false },
      -- Keep quickfix/source jumps from replacing the CLI terminal buffer.
      wo = { winfixbuf = true },
      config = function(terminal)
        -- Sidekick uses nvim_put for both text and submit, wrapping Enter in a
        -- bracketed paste. Keep text pasted, but send Enter as a real keypress
        -- for every CLI (OpenCode otherwise reads the clipboard on empty paste).
        terminal.on_ready = function(self)
          self.timer:start(0, 100, function()
            local input = table.remove(self.send_queue, 1)
            if not input then
              return
            end

            input = input:gsub("\r\n", "\n")
            vim.schedule(function()
              if not self:is_running() then
                return
              end

              local bytes = input == "\r" and input or ("\27[200~" .. input .. "\27[201~")
              vim.api.nvim_chan_send(self.job, bytes)
              if self:is_focused() then
                vim.cmd.startinsert()
              end
            end)
          end)
        end
      end,
    },
    -- Neovim's terminal handles OSC52 directly, not tmux passthrough escapes.
    tools = {
      opencode_naia = {
        cmd = { sidekick_naia_wrapper, "opencode" },
        env = { TMUX = false, TMUX_PANE = false },
        is_proc = "\\<opencode\\>",
        continue = { "--continue" },
        native_scroll = true,
        url = "https://github.com/sst/opencode",
      },
      codex_naia = {
        cmd = { sidekick_naia_wrapper, "codex" },
        env = { TMUX = false, TMUX_PANE = false },
        is_proc = "\\<codex\\>",
        resume = { "resume" },
        continue = { "resume", "--last" },
        url = "https://github.com/openai/codex",
      },
      codex_naia_deepseek = {
        cmd = { sidekick_naia_wrapper, "codex", "-p", "deepseek" },
        env = { TMUX = false, TMUX_PANE = false },
        is_proc = "\\<codex\\>",
        resume = { "resume" },
        continue = { "resume", "--last" },
        url = "https://github.com/openai/codex",
      },
      claude_naia = {
        cmd = { sidekick_naia_wrapper, "claude" },
        env = { TMUX = false, TMUX_PANE = false },
        is_proc = "\\<claude\\>",
        continue = { "--continue" },
        native_scroll = true,
        url = "https://github.com/anthropics/claude-code",
      },
    },
  },
})

vim.keymap.set('n', '<c-p><c-g>', ':Sidekick cli toggle<CR>')

vim.api.nvim_create_user_command("AI", function()
  require("sidekick.cli").toggle("opencode_naia")
end, {
  desc = "Open Sidekick with the codex_naia tool",
})

-- Get git root if available
local function get_git_root(dir)
  local cmd = "git -C " .. vim.fn.shellescape(dir) .. " rev-parse --show-toplevel"

  local ok, lines = pcall(vim.fn.systemlist, cmd)
  if not ok or vim.v.shell_error ~= 0 or type(lines) ~= "table" or #lines == 0 then
    return nil
  end

  local root = lines[1]
  if root == nil or root == "" then
    return nil
  end

  return root
end

-- Use workspace-relative paths only for files inside the current workspace.
local function get_repo_relative_path()
  local bufname = vim.api.nvim_buf_get_name(0)
  if bufname == "" then
    return "[No Name]"
  end

  local abs_buf = vim.fn.fnamemodify(bufname, ":p")
  local cwd = vim.fn.getcwd()
  local workspace_root = get_git_root(cwd) or cwd
  local rel = vim.fs.relpath(workspace_root, abs_buf)
  if rel and rel ~= ".." and not rel:match("^%.%.[/\\]") then
    return rel == "" and "." or rel
  end

  return abs_buf
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

local function send_raw_ai_message(msg)
  local trimmed = vim.trim(msg or "")
  if trimmed == "" then
    vim.notify("AIMessage requires a message", vim.log.levels.ERROR)
    return false
  end

  if require("config.ai_tuios").send(msg) then
    return true
  end

  pcall(require("sidekick.cli").send, {
    msg = msg,
    submit = true,
  })

  return true
end

vim.api.nvim_create_user_command("AIMessage", function(opts)
  send_raw_ai_message(opts.args or "")
end, {
  nargs = "+",
  desc = "Send raw text to a tuios workspace agent, or submit to Sidekick",
})

-- Send metadata: file + line or line range (line info after a space)
vim.api.nvim_create_user_command("AISend", function(opts)
  local msg = build_ai_message(vim.tbl_extend("force", opts, {
    separator = ":",
  }))

  if not require("config.ai_tuios").send(msg) then
    require("sidekick.cli").send({
      msg = msg,
      submit = false,
    })
  end

  maybe_restore_visual(opts)
end, {
  nargs = "*",
  range = true,
  desc = "Send file/line context to a tuios workspace agent, or insert into Sidekick",
})

vim.api.nvim_create_user_command("AICommit", function()
  vim.api.nvim_cmd({
    cmd = "AIMessage",
    args = { "Git commit staged changes only, decide a commit message based only on those changes" },
  }, {})
end, {
  desc = "Send git commit staged prompt to Sidekick",
})

vim.api.nvim_create_user_command("AICommitSkill", function()
  vim.api.nvim_cmd({
    cmd = "AIMessage",
    args = { "Run the git-commit-staged skill." },
  }, {})
end, {
  desc = "Send git commit staged prompt to Sidekick",
})

vim.api.nvim_create_user_command("AICommitSkillAuto", function()
  vim.api.nvim_cmd({
    cmd = "AIMessage",
    args = { "Run the git-commit-staged skill, automated." },
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
