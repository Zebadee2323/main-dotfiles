-- Tabs and window resizing.
vim.keymap.set("n", "<A-Tab>", "<CMD>tabnext<CR>")
vim.keymap.set("n", "<Left>", "<CMD>vertical resize +3<CR>")
vim.keymap.set("n", "<Right>", "<CMD>vertical resize -3<CR>")
vim.keymap.set("n", "<Up>", "<CMD>resize -3<CR>")
vim.keymap.set("n", "<Down>", "<CMD>resize +3<CR>")

-- Move selections and keep search matches centred.
vim.keymap.set("v", "J", ":m '>+1<CR>gv=gv")
vim.keymap.set("v", "K", ":m '<-2<CR>gv=gv")
vim.keymap.set("n", "n", "nzzzv")
vim.keymap.set("n", "N", "Nzzzv")

-- Explicit clipboard shortcuts; standard editing keys stay vanilla.
vim.keymap.set("x", "<D-c>", '"+y', { desc = "Copy selection to system clipboard" })
vim.keymap.set("x", "<F12>", '"+y', { desc = "Copy selection from Kitty/tmux" })

vim.keymap.set("n", "<C-k>", "<cmd>cprev<cr>zz")
vim.keymap.set("n", "<C-j>", "<cmd>cnext<cr>zz")
vim.keymap.set("n", "<C-s><C-s><C-s>", vim.cmd.TrimWhitespace)

vim.keymap.set("t", "<C-w>h", [[<C-\><C-n><C-w>h]])
vim.keymap.set("t", "<C-w><C-h>", [[<C-\><C-n><C-w>h]])
vim.keymap.set("t", "<C-w>l", [[<C-\><C-n><C-w>l]])
vim.keymap.set("t", "<C-w><C-l>", [[<C-\><C-n><C-w>l]])
vim.keymap.set("t", "<C-w>j", [[<C-\><C-n><C-w>j]])
vim.keymap.set("t", "<C-w><C-j>", [[<C-\><C-n><C-w>j]])
vim.keymap.set("t", "<C-w>k", [[<C-\><C-n><C-w>k]])
vim.keymap.set("t", "<C-w><C-k>", [[<C-\><C-n><C-w>k]])
