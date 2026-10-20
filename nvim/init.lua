-- Exclude legacy Packer packages before the first require() can discover them.
-- Lazy manages all third-party plugins; keep Neovim's bundled packages available.
vim.opt.packpath = { vim.env.VIMRUNTIME }

require('core')
require('config.lazy')
