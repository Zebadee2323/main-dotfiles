local lsp_zero = require('lsp-zero')
local blink_caps = require('blink.cmp').get_lsp_capabilities()

if not vim.g._naia_lsp_zero_client_configured then
    lsp_zero.client_config({
        capabilities = blink_caps,
        on_init = function(client)
            client.server_capabilities.semanticTokensProvider = nil
            client.server_capabilities.workspace.didChangeWatchedFiles.dynamicRegistration = false
        end,
    })
    vim.g._naia_lsp_zero_client_configured = true
end

vim.lsp.config('lua_ls', {
    capabilities = blink_caps,
    settings = {
        Lua = {
            diagnostics = {
                globals = { 'vim' },
                disable = { 'unused-function', 'unused-local' },
            },
            workspace = {
                checkThirdParty = false,
                library = vim.api.nvim_get_runtime_file('', true),
            },
        },
    },
})

if not vim.g._naia_lua_ls_enabled then
    vim.lsp.enable('lua_ls')
    vim.g._naia_lua_ls_enabled = true
end

local lsp_attach_group = vim.api.nvim_create_augroup('NaiaLspAttach', { clear = true })
vim.api.nvim_create_autocmd('LspAttach', {
    group = lsp_attach_group,
    callback = function(args)
        local bufopts = { noremap = true, silent = true, buffer = args.buf }
        vim.keymap.set('n', '<C-l><C-l>', vim.lsp.buf.definition, bufopts)
        vim.keymap.set('n', '<C-l><C-e>', vim.lsp.buf.hover, bufopts)
        vim.keymap.set('n', '<C-l><C-w>', vim.diagnostic.open_float, bufopts)
        vim.keymap.set('n', '<C-l><C-i>', vim.lsp.buf.implementation, bufopts)
        vim.keymap.set('n', '<C-l><C-r>', vim.lsp.buf.rename, bufopts)
        vim.keymap.set('n', '<C-l><C-a>', vim.lsp.buf.code_action, bufopts)
        vim.keymap.set('n', '<C-l><C-o>', vim.lsp.buf.references, bufopts)
        vim.keymap.set('n', '==', vim.lsp.buf.format, bufopts)
        vim.keymap.set('v', '==', vim.lsp.buf.format, bufopts)
    end,
})

if not vim.g._naia_mason_lspconfig_setup_done then
    require('mason-lspconfig').setup({
        automatic_enable = {
            exclude = { 'lua_ls' },
        },
    })
    vim.g._naia_mason_lspconfig_setup_done = true
end

local function create_or_replace_user_command(name, fn, opts)
    pcall(vim.api.nvim_del_user_command, name)
    vim.api.nvim_create_user_command(name, fn, opts or {})
end

local function format_diagnostic(diagnostic)
    local row = diagnostic.lnum + 1
    local col = diagnostic.col + 1
    return string.format('%d:%d %s', row, col, diagnostic.message)
end

local function copy_diagnostic_under_cursor()
    local line = vim.api.nvim_win_get_cursor(0)[1] - 1
    local diagnostics = vim.diagnostic.get(0, { lnum = line })

    if #diagnostics == 0 then
        vim.notify('No diagnostic under cursor', vim.log.levels.WARN)
        return
    end

    local text = format_diagnostic(diagnostics[1])
    vim.fn.setreg('+', text)
    vim.fn.setreg('"', text)
    vim.notify('Copied diagnostic under cursor')
end

local function copy_all_buffer_diagnostics()
    local diagnostics = vim.diagnostic.get(0)

    if #diagnostics == 0 then
        vim.notify('No diagnostics in current buffer', vim.log.levels.WARN)
        return
    end

    table.sort(diagnostics, function(a, b)
        if a.lnum == b.lnum then
            return a.col < b.col
        end
        return a.lnum < b.lnum
    end)

    local severity_names = {
        [vim.diagnostic.severity.ERROR] = 'ERROR',
        [vim.diagnostic.severity.WARN] = 'WARN',
        [vim.diagnostic.severity.INFO] = 'INFO',
        [vim.diagnostic.severity.HINT] = 'HINT',
    }

    local lines = {}
    for _, diagnostic in ipairs(diagnostics) do
        local severity = severity_names[diagnostic.severity] or 'UNKNOWN'
        table.insert(lines, string.format('%s [%s]', format_diagnostic(diagnostic), severity))
    end

    local text = table.concat(lines, '\n')
    vim.fn.setreg('+', text)
    vim.fn.setreg('"', text)
    vim.notify(string.format('Copied %d diagnostics', #diagnostics))
end

create_or_replace_user_command('CopyDiagnostic', copy_diagnostic_under_cursor, {})
create_or_replace_user_command('CopyAllDiagnostics', copy_all_buffer_diagnostics, {})
