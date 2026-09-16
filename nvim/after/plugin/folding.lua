-- Folding -----------------------------------------------------------------------------------------------------------
-- Global default is 'manual' (see :h foldmethod); we opt specific filetypes into
-- richer fold methods below. Global 'foldlevel' is 99 (editor.lua) so nothing
-- starts folded unless a buffer overrides it.

-- Fugitive commit/diff buffers (filetype=git): the git *syntax* defines a fold
-- region per file in the diff, but only 'foldmethod=syntax' activates them.
-- With foldlevel=1 each file's diff starts expanded but collapsible: use zM to
-- fold every file down to its header, za/zo to expand the ones you want.
vim.api.nvim_create_autocmd("FileType", {
  pattern = "git",
  callback = function()
    vim.opt_local.foldmethod = "syntax"
    vim.opt_local.foldlevel = 1
  end,
})
