return {
    'tpope/vim-commentary',
    'justinmk/vim-sneak',
    {
        'folke/snacks.nvim',
        lazy = false,
        opts = {
            scroll = {
                enabled = not vim.g.neovide,
            },
        },
    },
    {
        'mvllow/modes.nvim',
        config = function()
            require('config.modes')
        end,
    },
}
