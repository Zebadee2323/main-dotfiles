local M = {}

-- DAP's built-in `uselast` can try to replace a winfixbuf-protected buffer.
-- Keep navigation in ordinary source windows without unlocking utility panes.
function M.switchbuf(bufnr, line, column)
    local api = vim.api
    local current = api.nvim_get_current_win()
    local keep_focus = vim.bo[api.nvim_win_get_buf(current)].filetype == "dap-repl"
    local windows = { current }
    for _, win in ipairs(api.nvim_tabpage_list_wins(0)) do
        if win ~= current then windows[#windows + 1] = win end
    end

    local function is_editor(win)
        local buf = api.nvim_win_get_buf(win)
        return api.nvim_win_get_config(win).relative == "" and vim.bo[buf].buftype == ""
    end

    local target
    -- A locked source window is safe when it already contains the target buffer.
    for _, win in ipairs(windows) do
        if is_editor(win) and api.nvim_win_get_buf(win) == bufnr then
            target = win
            break
        end
    end
    if not target then
        for _, win in ipairs(windows) do
            local buf = api.nvim_win_get_buf(win)
            if is_editor(win) and not vim.wo[win].winfixbuf
                and not vim.wo[win].diff and vim.bo[buf].modifiable then
                target = win
                break
            end
        end
    end
    if not target then
        vim.cmd("belowright vsplit")
        target = api.nvim_get_current_win()
        -- A new split may inherit its parent's lock; never change the parent.
        vim.wo[target].winfixbuf = false
    end

    if api.nvim_win_get_buf(target) ~= bufnr then
        api.nvim_win_set_buf(target, bufnr)
    end
    local last_line = api.nvim_buf_line_count(bufnr)
    line = math.max(1, math.min(line, last_line))
    local text = api.nvim_buf_get_lines(bufnr, line - 1, line, false)[1] or ""
    api.nvim_win_set_cursor(target, { line, math.max(0, math.min((column or 1) - 1, #text)) })
    api.nvim_win_call(target, function() vim.cmd("normal! zv") end)
    api.nvim_set_current_win(keep_focus and current or target)
end

return M
