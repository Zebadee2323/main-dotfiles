-- User-command wiring, separate from playback state and transitions.
local M = {}

function M.setup(actions)
  local function command(name, callback, description, opts)
    vim.api.nvim_create_user_command(name, callback, vim.tbl_extend("force", {
      desc = description, force = true,
    }, opts or {}))
  end

  command("AIWalkStop", actions.stop, "Stop the active AI walkthrough")
  command("AIWalkRepeat", function() actions.repeat_tour(false) end, "Repeat the last AI walkthrough")
  command("AIWalkRepeatSlow", function() actions.repeat_tour(true) end,
    "Repeat the last AI walkthrough, pausing after each step")
  command("AIWalkNext", actions.next, "Advance a paused AI walkthrough by one step")
  command("AIWalkContinue", function(opts)
    local step = vim.trim(opts.args)
    actions.resume(step ~= "" and step or nil)
  end, "Resume automatic AI walkthrough playback, optionally pausing again at a step", { nargs = "?" })
  command("AIWalkToggle", actions.toggle, "Toggle AI walkthrough playback")
  command("AIWalkPause", actions.pause, "Pause the active AI walkthrough after the current step")
  command("AIWalkPrev", actions.previous, "Replay the previous AI walkthrough step")
  command("AIWalkPrevious", actions.previous, "Replay the previous AI walkthrough step")
  command("AIWalkParent", actions.parent, "Jump to the parent AI walkthrough step")
  command("AIWalkThread", function(opts)
    vim.api.nvim_cmd({ cmd = "AIMessage", args = { "For the current walkthrough step:\n" .. vim.trim(opts.args) } }, {})
  end, "Send a walkthrough-step prompt via AIMessage", { nargs = "*" })
  command("AIWalkInstruct", actions.instruct, "Send an instruction to AI using the current walkthrough step as context",
    { nargs = "*" })
  command("AIWalkRemoveEnquiry", actions.remove_enquiry, "Remove enquiry steps for the current walkthrough branch")
  command("AIWalkRestart", actions.restart, "Restart the active AI walkthrough from the first step")
  command("AIWalkWindow", actions.browser, "Open a floating window for browsing AI walkthrough steps")
  command("AIWalkExport", function(opts) actions.export(opts.args, opts.bang) end,
    "Export portable walkthrough JSON (! overwrites)", { bang = true, nargs = 1, complete = "file" })
  command("AIWalkImport", function(opts) actions.import(opts.args) end,
    "Load portable JSON or a trusted legacy YAML walkthrough", { nargs = 1, complete = "file" })
  command("AIWalkLoad", function(opts) actions.import(opts.args) end,
    "Load a walkthrough (alias for AIWalkImport)", { nargs = 1, complete = "file" })
end

return M
