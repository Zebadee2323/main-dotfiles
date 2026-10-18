return {
    {
        'stevearc/oil.nvim',
        dependencies = { 'nvim-tree/nvim-web-devicons' },
        config = function()
            require('config.oil')
        end,
    },
    {
        'refractalize/oil-git-status.nvim',
        dependencies = { 'stevearc/oil.nvim' },
        opts = {},
    },
}
