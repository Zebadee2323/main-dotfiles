-- Bootstrap lazy.nvim. Existing Packer packages can remain on disk: Lazy resets
-- runtimepath/packpath and owns plugin loading, so they won't be loaded twice.
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
    -- Telescope
    {
        'nvim-telescope/telescope.nvim',
        dependencies = {
            'nvim-lua/plenary.nvim',
            { 'nvim-telescope/telescope-fzf-native.nvim', build = 'make' },
        },
    },

    -- File Management
    {
        'stevearc/oil.nvim',
        dependencies = { 'nvim-tree/nvim-web-devicons' },
    },
    {
        'refractalize/oil-git-status.nvim',
        dependencies = { 'stevearc/oil.nvim' },
    },

    -- Status Bar
    {
        'nvim-lualine/lualine.nvim',
        dependencies = { 'nvim-tree/nvim-web-devicons', 'linrongbin16/lsp-progress.nvim' },
    },

    -- AI
    'github/copilot.vim',
    {
        'folke/sidekick.nvim',
        dependencies = { 'nvim-naia' },
    },
    {
        'olimorris/codecompanion.nvim',
        dependencies = { 'nvim-lua/plenary.nvim', 'nvim-treesitter/nvim-treesitter', 'nvim-naia' },
    },
    {
        dir = '/Users/onelson/Development/nvim-naia',
        name = 'nvim-naia',
        config = function()
            require('naia').setup()
        end,
    },

    -- Color Themes (the active theme is still selected in after/plugin/colorscheme.lua)
    'morhetz/gruvbox',
    'folke/tokyonight.nvim',
    'sainnhe/everforest',
    'rebelot/kanagawa.nvim',
    'shaunsingh/nord.nvim',
    'Yazeed1s/oh-lucy.nvim',
    'ficcdaf/ashen.nvim',
    'aliqyan-21/darkvoid.nvim',

    -- Animation
    'karb94/neoscroll.nvim',

    -- Cursor Mode Highlighting
    'mvllow/modes.nvim',

    -- Git
    'tpope/vim-fugitive',
    {
        'kdheepak/lazygit.nvim',
        dependencies = { 'nvim-lua/plenary.nvim' },
    },
    'lewis6991/gitsigns.nvim',

    -- Lsp
    'neovim/nvim-lspconfig',
    'VonHeikemen/lsp-zero.nvim',
    {
        'linrongbin16/lsp-progress.nvim',
        config = function()
            require('lsp-progress').setup()
        end,
    },

    -- Lsp Status
    'j-hui/fidget.nvim',

    -- Mason
    {
        'mason-org/mason.nvim',
        config = function()
            require('mason').setup({
                registries = {
                    'github:mason-org/mason-registry',
                    'github:Crashdummyy/mason-registry', -- adds roslyn + rzls packages
                },
            })
        end,
    },
    {
        'mason-org/mason-lspconfig.nvim',
        dependencies = { 'mason-org/mason.nvim', 'neovim/nvim-lspconfig' },
    },

    -- Roslyn helpers
    'seblyng/roslyn.nvim',

    -- Debugging
    'mfussenegger/nvim-dap',
    {
        'rcarriga/nvim-dap-ui',
        dependencies = { 'mfussenegger/nvim-dap', 'nvim-neotest/nvim-nio' },
    },
    'nvim-neotest/nvim-nio',
    {
        'theHamsta/nvim-dap-virtual-text',
        dependencies = { 'mfussenegger/nvim-dap', 'nvim-treesitter/nvim-treesitter' },
    },

    -- Auto Completion (latest Blink requires Neovim 0.12+ and blink.lib)
    {
        'saghen/blink.cmp',
        dependencies = { 'saghen/blink.lib' },
        build = function()
            require('blink.cmp').build():pwait()
        end,
    },

    -- Highlighting (Treesitter's main-branch API is used in after/plugin/treesitter.lua)
    {
        'nvim-treesitter/nvim-treesitter',
        branch = 'main',
        build = ':TSUpdate',
    },

    -- Commenting
    'tpope/vim-commentary',

    -- Motions
    'justinmk/vim-sneak',

    -- Markdown
    {
        'MeanderingProgrammer/render-markdown.nvim',
        dependencies = { 'nvim-treesitter/nvim-treesitter', 'nvim-tree/nvim-web-devicons' },
        config = function()
            require('render-markdown').setup({})
        end,
    },
}, {
    -- Preserve the Packer setup: load plugins at startup, then source the
    -- existing after/plugin files in their usual alphabetical order.
    defaults = { lazy = false, version = false },
    -- Don't let repository-local specs silently change this plugin list.
    local_spec = false,
    performance = { reset_packpath = true, rtp = { reset = true } },
})
