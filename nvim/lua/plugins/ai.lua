return {
    'github/copilot.vim',
    {
        'guill/mcp-tools.nvim',
        dependencies = { 'mfussenegger/nvim-dap' },
        build = 'cd bridge && npm install',
        config = function()
            require('config.mcp_tools')
        end,
    },
    {
        dir = '/Users/onelson/Development/nvim-naia',
        name = 'nvim-naia',
        dependencies = { 'guill/mcp-tools.nvim' },
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
