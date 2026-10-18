return {
    {
        'tpope/vim-fugitive',
        config = function()
            require('config.fugitive')
        end,
    },
    {
        'kdheepak/lazygit.nvim',
        dependencies = { 'nvim-lua/plenary.nvim' },
    },
    {
        'lewis6991/gitsigns.nvim',
        config = function()
            require('config.git-signs')
        end,
    },
}
