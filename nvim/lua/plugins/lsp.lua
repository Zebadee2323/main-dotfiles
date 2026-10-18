return {
    'neovim/nvim-lspconfig',
    'VonHeikemen/lsp-zero.nvim',
    {
        'mason-org/mason.nvim',
        opts = {
            registries = {
                'github:mason-org/mason-registry',
                'github:Crashdummyy/mason-registry', -- Roslyn + rzls packages
            },
        },
    },
    {
        'mason-org/mason-lspconfig.nvim',
        dependencies = {
            'mason-org/mason.nvim',
            'neovim/nvim-lspconfig',
            'VonHeikemen/lsp-zero.nvim',
            'saghen/blink.cmp',
        },
        config = function()
            require('config.lsp')
        end,
    },
    {
        'linrongbin16/lsp-progress.nvim',
        opts = {},
    },
    {
        'j-hui/fidget.nvim',
        config = function()
            require('config.fidget')
        end,
    },
}
