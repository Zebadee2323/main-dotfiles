return {
    'tpope/vim-commentary',
    'justinmk/vim-sneak',
    {
        'karb94/neoscroll.nvim',
        config = function()
            require('config.neoscroll')
        end,
    },
    {
        'mvllow/modes.nvim',
        config = function()
            require('config.modes')
        end,
    },
}
