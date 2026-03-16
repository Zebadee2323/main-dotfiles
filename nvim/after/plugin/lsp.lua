local lsp_zero = require('lsp-zero')
local blink_caps = require('blink.cmp').get_lsp_capabilities()
local default_references_handler = vim.lsp.handlers['textDocument/references']

vim.lsp.handlers['textDocument/references'] = function(err, result, ctx, config)
    local ok, response = pcall(default_references_handler, err, result, ctx, config)
    if ok then
        return response
    end

    if type(result) ~= 'table' then
        error(response)
    end

    local filtered = vim.tbl_filter(function(item)
        return type(item) ~= 'table'
            or type(item.uri) ~= 'string'
            or not vim.startswith(item.uri, 'roslyn-source-generated://')
    end, result)

    if #filtered == #result then
        error(response)
    end

    vim.schedule(function()
        vim.notify(
            'Skipped source-generated Roslyn references that could not be loaded',
            vim.log.levels.WARN
        )
    end)

    return default_references_handler(err, filtered, ctx, config)
end

lsp_zero.set_server_config({
    capabilities = blink_caps,
    on_init = function(client)
        -- your existing tweaks
        client.server_capabilities.semanticTokensProvider = nil
        client.server_capabilities.workspace.didChangeWatchedFiles.dynamicRegistration = false
    end,
})

-- 3) Keep your on_attach (unchanged except we don’t double-bind signature_help)
lsp_zero.on_attach(function(client, bufnr)
    local bufopts = { noremap = true, silent = true, buffer = bufnr }
    --vim.keymap.set('n', '<C-l><C-l>', vim.lsp.buf.declaration, bufopts)
    vim.keymap.set('n', '<C-l><C-l>', vim.lsp.buf.definition, bufopts)
    vim.keymap.set('n', '<C-l><C-e>', vim.lsp.buf.hover, bufopts)
    vim.keymap.set('n', '<C-l><C-w>', vim.diagnostic.open_float, bufopts)
    vim.keymap.set('n', '<C-l><C-i>', vim.lsp.buf.implementation, bufopts)

    -- We now use blink on <C-l><C-k> (configured above), so remove native binds here:
    -- vim.keymap.set('n', '<C-l><C-k>', vim.lsp.buf.signature_help, bufopts)
    -- vim.keymap.set('i', '<C-l><C-k>', vim.lsp.buf.signature_help, bufopts)

    --vim.keymap.set('n', '<space>wa', vim.lsp.buf.add_workspace_folder, bufopts)
    --vim.keymap.set('n', '<space>wr', vim.lsp.buf.remove_workspace_folder, bufopts)
    --vim.keymap.set('n', '<space>wl', function() print(vim.inspect(vim.lsp.buf.list_workspace_folders())) end, bufopts)
    --vim.keymap.set('n', '<space>D', vim.lsp.buf.type_definition, bufopts)
    vim.keymap.set('n', '<C-l><C-r>', vim.lsp.buf.rename, bufopts)
    vim.keymap.set('n', '<C-l><C-a>', vim.lsp.buf.code_action, bufopts)
    vim.keymap.set('n', '<C-l><C-o>', vim.lsp.buf.references, bufopts)
    vim.keymap.set('n', '==', vim.lsp.buf.format, bufopts)
    vim.keymap.set('v', '==', vim.lsp.buf.format, bufopts)
end)

-- 4) Your existing mason-lspconfig glue stays the same
require('mason-lspconfig').setup({
    handlers = {
        lsp_zero.default_setup,
    },
})
