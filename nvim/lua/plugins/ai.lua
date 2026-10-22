return {
    'github/copilot.vim',
    {
        dir = vim.fn.expand('~/dev/naia.nvim'),
        name = 'nvim-naia',
        config = function()
            require('naia').setup()
            require('config.naia')
        end,
    },
    {
        'folke/sidekick.nvim',
        dependencies = { 'nvim-naia' },
        config = function()
            require('config.sidekick')
        end,
    },
    {
        'olimorris/codecompanion.nvim',
        dependencies = {
            'nvim-lua/plenary.nvim',
            'nvim-treesitter/nvim-treesitter',
            'nvim-naia',
        },
        config = function()
            require('config.code-companion')
        end,
    },
}
