-- init.lua or lua/plugin/fugitive_branch_dd.lua

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
