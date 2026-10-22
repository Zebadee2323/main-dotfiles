local function unity_roslyn_rename()
    local curr_buf = vim.api.nvim_get_current_buf()
    local old_name = vim.fn.expand('<cword>')
    local old_filename_no_ext = vim.fn.expand('%:t:r')
    local old_ext = vim.fn.expand('%:e')
    
    -- Check if we are actually in a C# file and if the cursor word matches the filename
    local is_same_name = (old_name == old_filename_no_ext) and (old_ext == 'cs')
    
    -- Prompt the user for the new name
    vim.ui.input({ prompt = 'Rename symbol (Unity): ', default = old_name }, function(new_name)
        if not new_name or new_name == "" or new_name == old_name then
            return
        end

        -- Prepare LSP rename parameters
        local params = vim.lsp.util.make_position_params()
        params.newName = new_name

        -- Send the Rename Request to the LSP
        vim.lsp.buf_request(curr_buf, 'textDocument/rename', params, function(err, result, ctx, _)
            if err then
                vim.notify("LSP Rename Error: " .. err.message, vim.log.levels.ERROR)
                return
            end

            if not result then return end

            -- 1. Apply the text edits from Roslyn (renames the class in code)
            local client = vim.lsp.get_client_by_id(ctx.client_id)
            vim.lsp.util.apply_workspace_edit(result, client.offset_encoding)

            -- 2. If the class name matched the file name, proceed with file renaming
            if is_same_name then
                local old_file_path = vim.api.nvim_buf_get_name(curr_buf)
                local parent_dir = vim.fn.expand('%:p:h')
                
                local new_file_name = new_name .. ".cs"
                local new_file_path = parent_dir .. "/" .. new_file_name
                
                -- Save the buffer to write the text changes (class NewName) to the old file
                vim.cmd('write')

                -- Rename the .cs file
                local success, rename_err = os.rename(old_file_path, new_file_path)
                if not success then
                    vim.notify("Failed to rename file: " .. rename_err, vim.log.levels.ERROR)
                    return
                end

                -- Rename the .meta file if it exists
                local old_meta_path = old_file_path .. ".meta"
                local new_meta_path = new_file_path .. ".meta"
                
                -- Check if meta exists using vim.loop (uv)
                local stat = vim.uv.fs_stat(old_meta_path) -- Use vim.loop for older nvim versions
                if stat then
                    os.rename(old_meta_path, new_meta_path)
                end

                -- 3. Update Neovim to look at the new file path
                vim.api.nvim_buf_set_name(curr_buf, new_file_path)
                
                -- Reload the buffer to ensure LSP attaches to the new file path correctly
                vim.cmd('edit!')
                
                vim.notify("Renamed " .. old_filename_no_ext .. " to " .. new_name .. " (including .meta)", vim.log.levels.INFO)
            end
        end)
    end)
end

-- Create a user command for easy access
vim.api.nvim_create_user_command('UnityRename', unity_roslyn_rename, {})
