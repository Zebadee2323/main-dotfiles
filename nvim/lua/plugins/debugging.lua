return {
    'mfussenegger/nvim-dap',
    'nvim-neotest/nvim-nio',
    {
        'theHamsta/nvim-dap-virtual-text',
        dependencies = {
            'mfussenegger/nvim-dap',
            'nvim-treesitter/nvim-treesitter',
        },
    },
    {
        'rcarriga/nvim-dap-ui',
        dependencies = {
            'mfussenegger/nvim-dap',
            'nvim-neotest/nvim-nio',
            'theHamsta/nvim-dap-virtual-text',
        },
        config = function()
            require('config.dap')
            require('config.dap-unity')
        end,
    },
}
