-- Fugitive commands and buffer-local shortcuts.

-- Opt-in polling for changes made outside Neovim. Only refresh repositories
-- whose :G summary is visible in the current tab; never open a window.
local refresh_timer
local refresh_interval = 5 -- seconds; deliberately disabled at startup

local function stop_refresh()
  if refresh_timer then
    vim.fn.timer_stop(refresh_timer)
    refresh_timer = nil
  end
end

local function start_refresh()
  stop_refresh()
  refresh_timer = vim.fn.timer_start(refresh_interval * 1000, function(timer)
    if timer ~= refresh_timer then return end
    local seen = {}
    for _, win in ipairs(vim.api.nvim_tabpage_list_wins(0)) do
      local buf = vim.api.nvim_win_get_buf(win)
      if vim.bo[buf].filetype == "fugitive" and vim.b[buf].fugitive_type == "index" then
        local dir = vim.fn.FugitiveGitDir(buf)
        if dir ~= "" and not seen[dir] then
          seen[dir] = true
          vim.fn.FugitiveDidChange(buf)
        end
      end
    end
  end, { ["repeat"] = -1 })
end

vim.api.nvim_create_user_command("GAutoRefresh", function(opts)
  local arg = opts.args
  if arg == "" or arg == "toggle" then
    if refresh_timer then stop_refresh() else start_refresh() end
  elseif arg == "off" then
    stop_refresh()
  elseif arg == "on" then
    start_refresh()
  elseif arg ~= "status" then
    local seconds = tonumber(arg)
    if not seconds or seconds < 1 or seconds > 86400 or seconds % 1 ~= 0 then
      vim.notify("Usage: :GAutoRefresh [on|off|toggle|status|seconds (1–86400)]", vim.log.levels.ERROR)
      return
    end
    refresh_interval = seconds
    start_refresh()
  end
  vim.notify(("Fugitive auto-refresh: %s (every %ds, visible status windows only)")
    :format(refresh_timer and "on" or "off", refresh_interval))
end, {
  nargs = "?",
  complete = function(lead)
    return vim.tbl_filter(function(value) return value:sub(1, #lead) == lead end,
      { "on", "off", "toggle", "status" })
  end,
  desc = "Toggle Fugitive status polling, or enable it with an interval in seconds",
})

vim.api.nvim_create_autocmd("VimLeavePre", { callback = stop_refresh })

local function trim(s)
  return (s:gsub("^%s+", ""):gsub("%s+$", ""))
end

local function confirm_yesno(prompt, default_no)
  local default = default_no and 2 or 1
  return vim.fn.confirm(prompt, "&Yes\n&No", default) == 1
end

local function branch_from_cursor()
  -- try <cfile> first (works well in fugitive branch lists)
  local cfile = trim(vim.fn.expand("<cfile>") or "")
  if cfile ~= "" then return cfile end

  -- fallback: parse the line
  local line = trim(vim.api.nvim_get_current_line())
  if line == "" then return nil end
  if line:match("^%(") then return nil end -- (HEAD detached...) etc

  line = line:gsub("^[%*%+%-]%s+", "")
  line = trim(line)
  if line == "" then return nil end
  return line
end

local function run_git(args)
  -- Prefer Fugitive :Git (so output stays in Fugitive UX)
  if vim.fn.exists(":Git") == 2 then
    vim.cmd("Git " .. args)
    return true
  end

  local out = vim.fn.system("git " .. args)
  if vim.v.shell_error ~= 0 then
    vim.notify(("git %s failed:\n%s"):format(args, out), vim.log.levels.ERROR)
    return false
  end
  return true
end

local function fugitive_branch_dd()
  local target = branch_from_cursor()
  if not target then
    -- If this isn't a branch line, do nothing (prevents accidental prompts in other pagers)
    return
  end

  -- If on a remote entry like "remotes/origin/foo"
  local remote_name, remote_branch = target:match("^remotes/([^/]+)/(.+)$")
  local local_branch = remote_branch or target

  local force = confirm_yesno(("Force-delete local branch '%s'? (No = safe -d)"):format(local_branch), true)
  local delete_remote = confirm_yesno("Delete remote branch too?", true)

  local remote, rbranch
  if delete_remote then
    if remote_name and remote_branch then
      remote, rbranch = remote_name, remote_branch
    else
      remote = trim(vim.fn.input("Remote name: ", "origin") or "")
      if remote == "" then
        vim.notify("Remote name empty; remote delete cancelled.", vim.log.levels.WARN)
        delete_remote = false
      else
        rbranch = local_branch
      end
    end
  end

  local cmds = {}
  table.insert(cmds, ("branch %s %s"):format(force and "-D" or "-d", vim.fn.shellescape(local_branch)))
  if delete_remote and remote and rbranch then
    table.insert(cmds, ("push %s --delete %s"):format(vim.fn.shellescape(remote), vim.fn.shellescape(rbranch)))
  end

  local summary = { "About to run:" }
  for _, c in ipairs(cmds) do
    table.insert(summary, "  :Git " .. c)
  end
  table.insert(summary, "")
  if not confirm_yesno(table.concat(summary, "\n") .. "Proceed?", true) then
    vim.notify("Cancelled.", vim.log.levels.INFO)
    return
  end

  for _, c in ipairs(cmds) do
    if not run_git(c) then return end
  end

  vim.notify(("Deleted '%s'%s"):format(
    local_branch,
    (delete_remote and remote and rbranch) and (" and remote " .. remote .. "/" .. rbranch) or ""
  ), vim.log.levels.INFO)

  -- Refresh the pager buffer
  pcall(vim.cmd, "edit")
end

-- Key: Fugitive's branch view is a *pager* buffer; map when FugitivePager fires.
vim.api.nvim_create_autocmd("User", {
  pattern = "FugitivePager",
  callback = function(ev)
    vim.keymap.set("n", "dd", fugitive_branch_dd, {
      buffer = ev.buf,
      silent = true,
      nowait = true,
      desc = "Fugitive pager: delete branch under cursor",
    })
  end,
})

if vim.fn.exists(":GGoneBranches") == 2 then
  vim.api.nvim_del_user_command("GGoneBranches")
end

vim.api.nvim_create_user_command("GGoneBranches", function()
  vim.cmd("G fetch --prune")
  vim.cmd("G branch -vv")
end, {
  desc = "Fetch/prune remotes and list local branches with upstream status",
})

-- Disable `-` in Fugitive buffers: it sits right next to `=` and gets hit by
-- accident. It only stages/unstages (nothing is lost), but `s` and `u` do the
-- same job unambiguously. Fugitive's ftplugin maps run first, so this wins.
vim.api.nvim_create_autocmd("FileType", {
  pattern = "fugitive",
  callback = function(ev)
    for _, mode in ipairs({ "n", "x" }) do
      vim.keymap.set(mode, "-", "<Nop>", {
        buffer = ev.buf,
        nowait = true,
        desc = "Fugitive: disabled (use s/u to stage/unstage)",
      })
    end
  end,
})

-- :Gv is :Git in a vertical split pinned to the far left. `topleft` (rather
-- than plain `vertical`) makes it full-height alongside every other window
-- instead of only splitting the current one.
vim.api.nvim_create_user_command("Gv", function(opts)
  vim.cmd("vertical topleft Git " .. opts.args)
end, {
  nargs = "*",
  complete = "customlist,fugitive#Complete",
  desc = "Fugitive :Git in a full-height vertical split on the far left",
})
