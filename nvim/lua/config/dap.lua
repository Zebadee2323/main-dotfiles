require("dapui").setup()
require("nvim-dap-virtual-text").setup {
    enabled = true,                     -- enable this plugin (the default)
    enabled_commands = true,            -- create commands DapVirtualTextEnable, DapVirtualTextDisable, DapVirtualTextToggle, (DapVirtualTextForceRefresh for refreshing when debug adapter did not notify its termination)
    highlight_changed_variables = true, -- highlight changed values with NvimDapVirtualTextChanged, else always NvimDapVirtualText
    highlight_new_as_changed = false,   -- highlight new variables in the same way as changed variables (if highlight_changed_variables)
    show_stop_reason = true,            -- show stop reason when stopped for exceptions
    commented = false,                  -- prefix virtual text with comment string
    only_first_definition = false,      -- only show virtual text at first definition (if there are multiple)
    all_references = true,              -- show virtual text on all all references of the variable (not only definitions)
    clear_on_continue = false,          -- clear virtual text on "continue" (might cause flickering when stepping)
    --- A callback that determines how a variable is displayed or whether it should be omitted
    --- @param variable Variable https://microsoft.github.io/debug-adapter-protocol/specification#Types_Variable
    --- @param buf number
    --- @param stackframe dap.StackFrame https://microsoft.github.io/debug-adapter-protocol/specification#Types_StackFrame
    --- @param node userdata tree-sitter node identified as variable definition of reference (see `:h tsnode`)
    --- @param options nvim_dap_virtual_text_options Current options for nvim-dap-virtual-text
    --- @return string|nil A text how the virtual text should be displayed or nil, if this variable shouldn't be displayed
    display_callback = function(variable, buf, stackframe, node, options)
        -- by default, strip out new line characters
        if options.virt_text_pos == 'inline' then
            return ' = ' .. variable.value:gsub("%s+", " ")
        else
            return variable.name .. ' = ' .. variable.value:gsub("%s+", " ")
        end
    end,
    -- position of virtual text, see `:h nvim_buf_set_extmark()`, default tries to inline the virtual text. Use 'eol' to set to end of line
    virt_text_pos = vim.fn.has 'nvim-0.10' == 1 and 'inline' or 'eol',

    -- experimental features:
    all_frames = false,     -- show virtual text for all stack frames not only current. Only works for debugpy on my machine.
    virt_lines = false,     -- show virtual lines instead of virtual text (will flicker!)
    virt_text_win_col = nil -- position the virtual text at a fixed window column (starting from the first text column) ,
    -- e.g. 80 to position at column 80, see `:h nvim_buf_set_extmark()`
}

-- nvim-dap + dap-ui user commands
local ok_dap, dap = pcall(require, "dap")
if not ok_dap then
    vim.notify("nvim-dap not found", vim.log.levels.ERROR)
    return
end

local ok_ui, dapui = pcall(require, "dapui")

-- Auto-open/close dap-ui if available
if ok_ui then
    dap.listeners.after.event_initialized["dapui_autoopen"]  = function() dapui.open() end
    dap.listeners.before.event_terminated["dapui_autoclose"] = function() dapui.close() end
    dap.listeners.before.event_exited["dapui_autoclose"]     = function() dapui.close() end
end

-- Helpers
local function with_ui(fn)
    return function(...)
        if ok_ui then return fn(...) end
        vim.notify("dap-ui not installed", vim.log.levels.WARN)
    end
end

-- Core session commands
vim.api.nvim_create_user_command("DapStart", function() dap.continue() end, { desc = "Start/Continue debugging" })
vim.api.nvim_create_user_command("DapStop", function() dap.terminate() end, { desc = "Terminate session" })
vim.api.nvim_create_user_command("DapRestart", function()
    dap.terminate({}, {}, function() dap.run_last() end)
end, { desc = "Terminate then run last config" })
vim.api.nvim_create_user_command("DapRunLast", function() dap.run_last() end, { desc = "Run last debug config" })
vim.api.nvim_create_user_command("DapPause", function() dap.pause() end, { desc = "Pause the debuggee" })

-- Stepping
vim.api.nvim_create_user_command("DapStepOver", function() dap.step_over() end, { desc = "Step over" })
vim.api.nvim_create_user_command("DapStepInto", function() dap.step_into() end, { desc = "Step into" })
vim.api.nvim_create_user_command("DapStepOut", function() dap.step_out() end, { desc = "Step out" })
vim.api.nvim_create_user_command("DapStepBack", function()
    if dap.step_back then dap.step_back() else vim.notify("Adapter doesn't support stepBack", vim.log.levels.WARN) end
end, { desc = "Step back (if supported)" })

-- Breakpoints
vim.api.nvim_create_user_command("DapToggleBreakpoint", function() dap.toggle_breakpoint() end,
    { desc = "Toggle breakpoint" })
vim.api.nvim_create_user_command("DapBreakpointCondition", function()
    dap.set_breakpoint(vim.fn.input("Condition: "))
end, { desc = "Conditional breakpoint" })
vim.api.nvim_create_user_command("DapBreakpointLog", function()
    dap.set_breakpoint(nil, nil, vim.fn.input("Log message: "))
end, { desc = "Logpoint breakpoint" })
vim.api.nvim_create_user_command("DapClearBreakpoints", function() dap.clear_breakpoints() end,
    { desc = "Clear all breakpoints" })

-- REPL
vim.api.nvim_create_user_command("DapReplOpen", function() dap.repl.open() end, { desc = "Open REPL" })
vim.api.nvim_create_user_command("DapReplClose", function() dap.repl.close() end, { desc = "Close REPL" })
vim.api.nvim_create_user_command("DapReplToggle", function()
    if dap.repl then
        -- crude toggle: try close; if it errors, open
        pcall(dap.repl.close); dap.repl.open()
    end
end, { desc = "Toggle REPL" })

-- dap-ui panes (guarded)
vim.api.nvim_create_user_command("DapUIOpen", with_ui(function() dapui.open() end), { desc = "Open dap-ui" })
vim.api.nvim_create_user_command("DapUIClose", with_ui(function() dapui.close() end), { desc = "Close dap-ui" })
vim.api.nvim_create_user_command("DapUIToggle", with_ui(function() dapui.toggle() end), { desc = "Toggle dap-ui" })

-- Widgets (hover/preview/scopes/frames)
vim.api.nvim_create_user_command("DapHover", function()
    require("dap.ui.widgets").hover()
end, { desc = "DAP hover under cursor" })

vim.api.nvim_create_user_command("DapPreview", function()
    require("dap.ui.widgets").preview()
end, { desc = "Preview expression under cursor" })

vim.api.nvim_create_user_command("DapScopes", function()
    local widgets = require("dap.ui.widgets")
    widgets.centered_float(widgets.scopes)
end, { desc = "Centered scopes view" })

vim.api.nvim_create_user_command("DapFrames", function()
    local widgets = require("dap.ui.widgets")
    widgets.centered_float(widgets.frames)
end, { desc = "Centered frames view" })

-- Convenience: quick attach that just runs `continue`
vim.api.nvim_create_user_command("DapAttach", function() dap.continue() end,
    { desc = "Attach/continue (uses current config)" })

-- Optional: print current adapter/config for debugging
vim.api.nvim_create_user_command("DapInfo", function()
    local ft = vim.bo.filetype
    local cfgs = dap.configurations[ft] or {}
    print(("DAP ft=%s, configs=%d"):format(ft, #cfgs))
end, { desc = "Show current filetype & config count" })
