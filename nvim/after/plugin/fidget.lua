require("fidget").setup({
    progress = {
        suppress_on_insert = false,
        ignore_done_already = true,
        clear_on_detach = function(client_id)
            local c = vim.lsp.get_client_by_id(client_id)
            return c and c.name or nil
        end,
        display = {
            progress_icon = { pattern = "dots" },
            done_icon = "✔",
            done_ttl = 1.5,
        },
        lsp = {
            progress_ringbuf_size = 0,
            log_handler = false,
        },
    },

    notification = {
        -- set to true if you want Fidget to intercept vim.notify()
        override_vim_notify = true,
        window = {
            border = "rounded",
            align = { bottom = true, right = true },
            avoid = { "NvimTree" },
        },
    },
})
