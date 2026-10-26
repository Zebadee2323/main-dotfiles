return {
    'github/copilot.vim',
    {
        'guill/mcp-tools.nvim',
        dependencies = { 'mfussenegger/nvim-dap' },
        build = 'cd bridge && npm install',
        config = function()
            require('config.mcp_tools')
            require('config.naia')
        end,
    },
    {
        'folke/sidekick.nvim',
        dependencies = { 'guill/mcp-tools.nvim' },
        config = function()
            require('config.sidekick')
        end,
    },
    {
        'olimorris/codecompanion.nvim',
        dependencies = {
            'nvim-lua/plenary.nvim',
            'nvim-treesitter/nvim-treesitter',
            'guill/mcp-tools.nvim',
        },
        config = function()
            require('config.code-companion')
        end,
    },
}
