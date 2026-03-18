-- plugins.lua
-- This file can be loaded by calling `lua require('plugins')` from your init.vim

-- Only required if you have packer configured as `opt`
vim.cmd [[packadd packer.nvim]]

return require('packer').startup(function(use)
    -- Packer can manage itself
    use 'wbthomason/packer.nvim'

    -- Telescope
    use {
        'nvim-telescope/telescope.nvim',
        requires = { { 'nvim-lua/plenary.nvim' } }
    }

    -- File Management
    use {
        "stevearc/oil.nvim",
        dependencies = {
            "nvim-tree/nvim-web-devicons"
        }
    }
    use 'refractalize/oil-git-status.nvim'

    -- Status Bar
    use {
        'nvim-lualine/lualine.nvim',
        requires = { 'nvim-tree/nvim-web-devicons', opt = true }
    }

    -- AI
    use 'github/copilot.vim'
    use 'folke/sidekick.nvim'
    use 'olimorris/codecompanion.nvim'

    -- Color Themes
    use 'morhetz/gruvbox'
    use 'folke/tokyonight.nvim'
    use 'sainnhe/everforest'
    use 'rebelot/kanagawa.nvim'
    use 'shaunsingh/nord.nvim'
    use 'Yazeed1s/oh-lucy.nvim'
    use 'ficcdaf/ashen.nvim'
    use 'aliqyan-21/darkvoid.nvim'

    -- Animation
    use 'karb94/neoscroll.nvim'

    -- Cursor Mode Highlighting
    use 'mvllow/modes.nvim'

    -- Git
    use 'tpope/vim-fugitive'
    use {
        'kdheepak/lazygit.nvim',
        requires = { { 'nvim-lua/plenary.nvim' } }
    }
    use 'lewis6991/gitsigns.nvim'

    -- Lsp
    use 'neovim/nvim-lspconfig'
    use 'VonHeikemen/lsp-zero.nvim'
    use {
        'linrongbin16/lsp-progress.nvim',
        config = function()
            require('lsp-progress').setup()
        end
    }

    -- Lsp Status
    use 'j-hui/fidget.nvim'

    -- Mason
    use {
        'mason-org/mason.nvim',
        config = function()
            require("mason").setup({
                registries = {
                    "github:mason-org/mason-registry",
                    "github:Crashdummyy/mason-registry", -- adds roslyn + rzls packages
                },
            })
        end
    }
    use 'mason-org/mason-lspconfig.nvim'

    -- Roslyn helpers
    use 'seblyng/roslyn.nvim'

    -- Debugging
    use 'mfussenegger/nvim-dap'
    use 'rcarriga/nvim-dap-ui'
    use 'nvim-neotest/nvim-nio'
    use 'theHamsta/nvim-dap-virtual-text'

    -- Auto Completion
    use {
        'saghen/blink.cmp',
        tag = 'v1.8.0',
    }

    -- Highlighting (Treesitter)
    use 'nvim-treesitter/nvim-treesitter'

    -- Commenting
    use 'tpope/vim-commentary'
end)
