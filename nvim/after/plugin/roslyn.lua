require("roslyn").setup({
    -- "auto" | "roslyn" | "off"
    --
    -- - "auto": Does nothing for filewatching, leaving everything as default
    -- - "roslyn": Turns off neovim filewatching which will make roslyn do the filewatching
    -- - "off": Hack to turn off all filewatching. (Can be used if you notice performance issues)
    filewatching = "roslyn",

    -- Optional function that takes an array of targets as the only argument. Return the target you
    -- want to use. If it returns `nil`, then it falls back to guessing the target like normal
    -- Example:
    --
    -- choose_target = function(target)
    --     return vim.iter(target):find(function(item)
    --         if string.match(item, "Foo.sln") then
    --             return item
    --         end
    --     end)
    -- end
    choose_target = nil,

    -- Optional function that takes the selected target as the only argument.
    -- Returns a boolean of whether it should be ignored to attach to or not
    --
    -- I am for example using this to disable a solution with a lot of .NET Framework code on mac
    -- Example:
    --
    -- ignore_target = function(target)
    --     return string.match(target, "Foo.sln") ~= nil
    -- end
    ignore_target = nil,

    -- Whether or not to look for solution files in the child of the (root).
    -- Set this to true if you have some projects that are not a child of the
    -- directory with the solution file
    broad_search = false,

    -- Whether or not to lock the solution target after the first attach.
    -- This will always attach to the target in `vim.g.roslyn_nvim_selected_solution`.
    -- NOTE: You can use `:Roslyn target` to change the target
    lock_target = false,

    -- If the plugin should silence notifications about initialization
    silent = false,
})

local source_generated_group = vim.api.nvim_create_augroup("NaiaRoslynSourceGenerated", { clear = true })

local function get_preferred_roslyn_clients(bufnr)
  local ordered = {}
  local seen = {}

  local function add(client)
    if not client or client.name ~= "roslyn" or seen[client.id] then
      return
    end
    seen[client.id] = true
    table.insert(ordered, client)
  end

  local preferred_client_id = vim.w.roslyn_source_generated_client_id
  if preferred_client_id then
    local preferred = vim.lsp.get_client_by_id(preferred_client_id)
    add(preferred)
  end

  for _, client in ipairs(vim.lsp.get_clients({ name = "roslyn", bufnr = bufnr })) do
    add(client)
  end

  for _, client in ipairs(vim.lsp.get_clients({ name = "roslyn" })) do
    add(client)
  end

  return ordered
end

local function open_source_generated_definition()
  local bufnr = vim.api.nvim_get_current_buf()
  local client = vim.lsp.get_clients({ name = "roslyn", bufnr = bufnr })[1]
  if not client then
    return vim.lsp.buf.definition()
  end

  local params = vim.lsp.util.make_position_params(0, client.offset_encoding)
  client:request("textDocument/definition", params, function(err, result)
    if err then
      vim.notify(err.message or tostring(err), vim.log.levels.ERROR)
      return
    end

    local locations = vim.islist(result) and result or result and { result } or {}
    if #locations ~= 1 then
      return vim.lsp.buf.definition()
    end

    local location = locations[1]
    local uri = location.uri or location.targetUri
    if not uri or not uri:match("^roslyn%-source%-generated://") then
      return vim.lsp.buf.definition()
    end

    vim.w.roslyn_source_generated_client_id = client.id
    vim.lsp.util.show_document(location, client.offset_encoding, { reuse_win = true, focus = true })
    vim.schedule(function()
      if vim.w.roslyn_source_generated_client_id == client.id then
        vim.w.roslyn_source_generated_client_id = nil
      end
    end)
  end, bufnr)
end

vim.api.nvim_create_autocmd("BufRead", {
  pattern = { "roslyn-source-generated://*" },
  callback = function(ev)
    vim.bo[ev.buf].buftype = "nofile"
    vim.bo[ev.buf].bufhidden = "wipe"
  end,
})

-- roslyn.nvim falls back to the first global Roslyn client for source-generated
-- buffers, which breaks when multiple Roslyn workspaces are alive in one session.
pcall(vim.api.nvim_clear_autocmds, {
  group = "roslyn.nvim",
  event = "BufReadCmd",
  pattern = "roslyn-source-generated://*",
})

vim.api.nvim_create_autocmd("BufReadCmd", {
  group = source_generated_group,
  pattern = "roslyn-source-generated://*",
  callback = function(args)
    vim.bo[args.buf].modifiable = true
    vim.bo[args.buf].swapfile = false
    vim.bo[args.buf].filetype = "cs"

    local clients = get_preferred_roslyn_clients(args.buf)
    local client = clients[1]
    if client then
      vim.lsp.buf_attach_client(args.buf, client.id)
    else
      vim.wait(5000, function()
        return next(vim.lsp.get_clients({ name = "roslyn", bufnr = args.buf })) ~= nil
      end)
      clients = get_preferred_roslyn_clients(args.buf)
      client = clients[1]
    end

    assert(client, "Must have a `roslyn` client to load roslyn source generated file")

    local content
    local tried = {}
    local function apply_result(result)
      content = result.text or ""
      if content == vim.NIL then
        content = ""
      end
      local normalized = string.gsub(content, "\r\n", "\n")
      local source_lines = vim.split(normalized, "\n", { plain = true })
      vim.api.nvim_buf_set_lines(args.buf, 0, -1, false, source_lines)
      vim.b[args.buf].resultId = result.resultId
      vim.bo[args.buf].modifiable = false
      vim.bo[args.buf].modified = false
    end

    local function request_from(index)
      local candidate = clients[index]
      if not candidate then
        vim.notify(
          string.format(
            "No Roslyn client could load source-generated document %s%s",
            args.match,
            next(tried) and (": " .. table.concat(tried, " | ")) or ""
          ),
          vim.log.levels.ERROR
        )
        content = ""
        vim.bo[args.buf].modifiable = false
        vim.bo[args.buf].modified = false
        return
      end

      vim.lsp.buf_attach_client(args.buf, candidate.id)
      candidate:request("sourceGeneratedDocument/_roslyn_getText", {
        textDocument = { uri = args.match },
        resultId = nil,
      }, function(err, result)
        if err then
          table.insert(tried, string.format("client %d: %s", candidate.id, err.message or vim.inspect(err)))
          return request_from(index + 1)
        end

        apply_result(result)
      end, args.buf)
    end

    request_from(1)

    vim.wait(1000, function()
      return content ~= nil
    end)
  end,
})

vim.api.nvim_create_autocmd("LspAttach", {
  group = source_generated_group,
  callback = function(args)
    local client = vim.lsp.get_client_by_id(args.data.client_id)
    if not client or client.name ~= "roslyn" then
      return
    end

    vim.keymap.set("n", "<C-l><C-l>", open_source_generated_definition, {
      noremap = true,
      silent = true,
      buffer = args.buf,
    })
  end,
})

-- :RoslynNudgeProject [optional/path/to/File.cs]
-- Finds the .csproj that references the file and notifies Roslyn to reload just that project.
-- Works well for Unity-generated .csproj files kept at the repo root.
do
    local function readfile(p)
        local f = io.open(p, 'rb'); if not f then return nil end
        local s = f:read('*a'); f:close(); return s
    end

    local function normslashes(s) -- forward slashes
        return (s:gsub('\\', '/'))
    end

    local function to_windows_slashes(s) -- backslashes
        return (s:gsub('/', '\\'))
    end

    local function lowercase(s)
        return s and s:lower() or s
    end

    local function relpath(root, abs)
        root = normslashes(root); abs = normslashes(abs)
        if abs:sub(1, #root) == root then
            local r = abs:sub(#root + 1)
            if r:sub(1, 1) == '/' then r = r:sub(2) end
            return r
        end
        return abs
    end

    local function roslyn_client()
        local clients = vim.lsp.get_active_clients({ name = 'roslyn' })
        return clients and clients[1] or nil
    end

    local function find_csproj_for_file(root, abs_file)
        local rel      = relpath(root, abs_file)
        local rel_unix = lowercase(normslashes(rel))
        local rel_win  = lowercase(to_windows_slashes(rel))

        local csprojs  = vim.fn.glob(root .. '/*.csproj', false, true) or {}
        for _, csproj in ipairs(csprojs) do
            local xml = readfile(csproj)
            if xml then
                local hay = lowercase(xml)
                -- Unity usually lists explicit Compile Include entries with relative paths.
                if hay:find(lowercase('include="' .. rel_unix .. '"'), 1, true)
                    or hay:find(lowercase('include="' .. rel_win .. '"'), 1, true)
                    or hay:find('<compile include="**/*.cs"', 1, true) -- SDK-style glob catch-all
                    or hay:find('<compile include="**\\*.cs"', 1, true) then
                    return csproj
                end
            end
        end
        return nil, csprojs -- return list for fallback
    end

    vim.api.nvim_create_user_command('RoslynRefreshFile', function(opts)
        local file = opts.args ~= '' and vim.fn.fnamemodify(opts.args, ':p') or vim.fn.expand('%:p')
        if file == '' then
            vim.notify('RoslynNudgeProject: no file', vim.log.levels.WARN); return
        end

        local client = roslyn_client()
        if not client then
            vim.notify('RoslynNudgeProject: roslyn LSP not active', vim.log.levels.ERROR); return
        end

        local root = client.config and client.config.root_dir or nil
        if not root or root == '' then
            vim.notify('RoslynNudgeProject: could not determine project root', vim.log.levels.ERROR); return
        end

        -- 1) Tell Roslyn the current file changed (safe even if just created)
        client.notify('workspace/didChangeWatchedFiles', {
            changes = { { uri = vim.uri_from_fname(file), type = 2 } }, -- 2 = Changed
        })

        -- 2) Find the owning .csproj and nudge it
        local csproj, all_csprojs = find_csproj_for_file(root, file)
        if csproj then
            client.notify('workspace/didChangeWatchedFiles', {
                changes = { { uri = vim.uri_from_fname(csproj), type = 2 } },
            })
            vim.notify('Roslyn nudged: ' .. csproj, vim.log.levels.INFO)
        else
            -- Fallback: nudge all root csprojs (Unity puts them all together)
            if all_csprojs and #all_csprojs > 0 then
                local changes = {}
                for _, p in ipairs(all_csprojs) do
                    table.insert(changes, { uri = vim.uri_from_fname(p), type = 2 })
                end
                client.notify('workspace/didChangeWatchedFiles', { changes = changes })
                vim.notify('RoslynNudgeProject: no explicit reference found; nudged all root .csproj files',
                    vim.log.levels.WARN)
            else
                vim.notify('RoslynNudgeProject: no .csproj files found at root', vim.log.levels.ERROR)
            end
        end
    end, { nargs = '?', complete = 'file' })
end
