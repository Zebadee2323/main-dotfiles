return {
    'morhetz/gruvbox',
    'folke/tokyonight.nvim',
    'sainnhe/everforest',
    'rebelot/kanagawa.nvim',
    'shaunsingh/nord.nvim',
    'Yazeed1s/oh-lucy.nvim',
    'aliqyan-21/darkvoid.nvim',
    {
        'ficcdaf/ashen.nvim',
        -- Apply the active theme before other plugins define their highlights.
        priority = 1000,
        config = function()
            require('config.colorscheme')
        end,
    },
}
