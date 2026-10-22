return {
    'seblyng/roslyn.nvim',
    -- Register the general LspAttach mappings before Roslyn overrides definition.
    dependencies = { 'mason-org/mason-lspconfig.nvim' },
    config = function()
        require('config.roslyn')
    end,
}
