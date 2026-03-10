local ts = require('telescope')
local actions = require('telescope.actions')
local builtin = require('telescope.builtin')
local themes = require('telescope.themes')

ts.setup {
    defaults = {
        mappings = {
            n = {
                ["<C-q><C-w>"] = actions.send_selected_to_qflist + actions.open_qflist,
                ["<C-q><C-q>"] = actions.send_to_qflist + actions.open_qflist,
                ["<C-d>"] = require('telescope.actions').results_scrolling_down,
                ["<C-b>"] = require('telescope.actions').results_scrolling_up,
            },
            i = {
                ["<C-q><C-w>"] = actions.send_selected_to_qflist + actions.open_qflist,
                ["<C-q><C-q>"] = actions.send_to_qflist + actions.open_qflist,
                ["<C-d>"] = require('telescope.actions').results_scrolling_down,
                ["<C-b>"] = require('telescope.actions').results_scrolling_up,
            }
        }
    }
}

vim.api.nvim_create_autocmd("FileType", {
    pattern = { "TelescopeResults" },
    callback = function()
        vim.opt_local.number = true
        vim.opt_local.relativenumber = true
    end,
})

function qfToLiveGrep()
    local files = {}
    local items = vim.fn.getqflist()
    for i = 1, #items do
        table.insert(files, vim.fn.bufname(items[i].bufnr))
    end
    builtin.live_grep({ layout_strategy = 'vertical', layout_config = { width = 0.95 }, search_dirs = files })
end

vim.keymap.set('n', '<C-p><C-p>', function()
    builtin.find_files(
        { 
            layout_strategy = 'vertical', 
            layout_config = { width = 0.95 },
            file_ignore_patterns = { "%.meta$" }
        })
end)
vim.keymap.set('n', '<C-p><C-f>', function()
    builtin.live_grep({ layout_strategy = 'vertical', layout_config = { width = 0.95 } })
end)
vim.keymap.set('n', '<C-p><C-s>', function()
    builtin.lsp_dynamic_workspace_symbols({ layout_strategy = 'vertical', layout_config = { width = 0.95 }, symbol_width = 60 })
end)
vim.keymap.set('n', '<C-p><C-b>', function() 
    builtin.buffers {
        sort_mru = true,
        previewer = false,
        -- ignore_current_buffer = true,
        attach_mappings = function(_, map)
            -- normal-mode `dd` or insert-mode `<C-d>` to delete the highlighted buffer
            map("n", "dd", actions.delete_buffer)
            map("i", "<C-d>", actions.delete_buffer)
            return true
        end,
    }
end)
vim.keymap.set('n', '<C-p><C-q>', function()
    builtin.quickfix({ layout_strategy = 'vertical', layout_config = { width = 0.95 }, fname_width = 150 })
end)
vim.keymap.set('n', '<C-p><C-t>', qfToLiveGrep)
