local blink = require('blink.cmp')
blink.setup({
    keymap = {
        preset = 'default',
        -- This makes Enter confirm the current completion item.
        ['<CR>'] = { 'accept', 'fallback' },
    },
    appearance = { nerd_font_variant = 'mono' },
    sources = { 
        default = { 'lsp', 'path', 'snippets', 'buffer' },
         -- IMPORTANT: extend the LSP source trigger characters
        providers = {
            lsp = {
                override = {
                    get_trigger_characters = function(self)
                        local chars = self:get_trigger_characters()
                        -- make sure these are treated as trigger chars even if Roslyn doesn't report them
                        vim.list_extend(chars, { '{', ',', '=' })
                        return chars
                    end,
                },
            },
        },
    },
    completion = {
        documentation = { auto_show = true },
        keyword_length = 0,

        menu = {
            -- just to be explicit; default is true
            auto_show = true,
        },

        trigger = {
            -- keep these as you had them
            show_on_insert = false,
            show_on_trigger_character = true,
            show_on_keyword = true,
            show_on_insert_on_trigger_character = true,

            -- don’t block any trigger characters (we’ll let Roslyn + our override drive it)
            show_on_blocked_trigger_characters = {},
            show_on_x_blocked_trigger_characters = {}, -- <- this removes the default "extra" block on '{', '[', etc
        },
    },
    fuzzy = { implementation = 'prefer_rust_with_warning' },

    -- Enable blink's signature help (experimental but works well)
    signature = {
        enabled = true,
        trigger = {
            enabled = true,
            show_on_trigger_character = true, -- pop on '(' and ','
            show_on_insert_on_trigger_character = true,
        },
        window = {
            show_documentation = true, -- just the signature lines
            border = "rounded",
            winhighlight = "NormalFloat:BlinkSignatureHelp,FloatBorder:FloatBorder",
        },
    },
})

vim.api.nvim_set_hl(0, "BlinkSignatureHelp", { bg = "#101010", fg = "#d0d0d0" })
vim.keymap.set('i', '<C-l><C-k>', blink.show, bufopts)
