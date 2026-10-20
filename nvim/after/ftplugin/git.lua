-- Git syntax defines a fold per diff file; start expanded but collapsible.
vim.opt_local.foldmethod = "syntax"
vim.opt_local.foldlevel = 1
vim.b.undo_ftplugin = (vim.b.undo_ftplugin and vim.b.undo_ftplugin .. " | " or "")
  .. "setlocal foldmethod< foldlevel<"
