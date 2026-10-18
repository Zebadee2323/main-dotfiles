-- Bootstrap lazy.nvim; plugin specs live in lua/plugins/.
local lazypath = vim.fn.stdpath('data') .. '/lazy/lazy.nvim'
if not (vim.uv or vim.loop).fs_stat(lazypath) then
    local output = vim.fn.system({
        'git', 'clone', '--filter=blob:none',
        'https://github.com/folke/lazy.nvim.git', lazypath,
    })
    if vim.v.shell_error ~= 0 then
        error('Failed to clone lazy.nvim:\n' .. output)
    end
end
vim.opt.rtp:prepend(lazypath)

-- Neovim's module loader may still have indexed the old Packer packages.
-- Clear that index before Lazy takes over runtimepath and plugin loading.
if vim.loader then
    vim.loader.reset()
end

require('lazy').setup({
    spec = { { import = 'plugins' } },
    -- Keep startup behavior unchanged during the migration. Individual specs
    -- can opt into lazy loading with lazy = true and keys/cmd/event/ft triggers.
    defaults = { lazy = false, version = false },
    -- Don't let repository-local specs silently change this plugin list.
    local_spec = false,
    performance = { reset_packpath = true, rtp = { reset = true } },
})
