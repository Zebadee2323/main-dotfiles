if vim.g.neovide then
    -- Start scale factor
    vim.g.neovide_scale_factor = vim.g.neovide_scale_factor or 1.0
    vim.g.neovide_opacity = 0.94
    vim.g.neovide_window_blurred = false

    local function change_scale(delta)
        vim.g.neovide_scale_factor = vim.g.neovide_scale_factor + delta
    end

    -- Ctrl+Shift+=  → zoom in
    vim.keymap.set({ "n", "v", "i" }, "<C-=>", function()
        change_scale(0.1)
    end, { desc = "Increase Neovide scale" })

    -- Ctrl+Shift+-  → zoom out
    vim.keymap.set({ "n", "v", "i" }, "<C-->", function()
        change_scale(-0.1)
    end, { desc = "Decrease Neovide scale" })

    -- Optional: Ctrl+0 → reset
    vim.keymap.set({ "n", "v", "i" }, "<C-0>", function()
        vim.g.neovide_scale_factor = 1.0
    end, { desc = "Reset Neovide scale" })

    -- macOS-style (Cmd)
    vim.keymap.set({ "n", "v" }, "<D-c>", '"+y', { desc = "Copy to system clipboard" })
    vim.keymap.set("n", "<D-v>", '"+P', { desc = "Paste from system clipboard" })
    vim.keymap.set("v", "<D-v>", '"+P', { desc = "Paste from system clipboard" })
    vim.keymap.set("c", "<D-v>", "<C-r>+", { desc = "Paste in command-line" })
    vim.keymap.set("i", "<D-v>", '<C-r><C-o>+', { desc = "Paste in insert mode" })
    vim.keymap.set("t", "<D-v>", '<C-\\><C-n>"+pa', { desc = "Paste in terminal mode" })

    -- Windows/Linux-style (Ctrl)
    vim.keymap.set({ "n", "v" }, "<C-S-c>", '"+y', { desc = "Copy to system clipboard" })
    vim.keymap.set("n", "<C-S-v>", '"+P', { desc = "Paste from system clipboard" })
    vim.keymap.set("v", "<C-S-v>", '"+P', { desc = "Paste from system clipboard" })
    vim.keymap.set("c", "<C-S-v>", "<C-r>+", { desc = "Paste in command-line" })
    vim.keymap.set("i", "<C-S-v>", '<C-r><C-o>+', { desc = "Paste in insert mode" })
    vim.keymap.set("t", "<C-S-v>", '<C-\\><C-n>"+pa', { desc = "Paste in terminal mode" })
end
