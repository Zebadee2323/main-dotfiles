-- Local compatibility guard for guill/mcp-tools.nvim, independent of Naia.
-- Upstream's SIGTERM handler sends qa! to Neovim. Own a direct Node/Bun child
-- and use SIGKILL until upstream fixes that handler. Keep the normal public
-- mcp-tools API/configuration, callbacks, registry and shared bridge intact.
local M = {}

function M.install()
    local bridge = require('mcp-tools.bridge')
    if bridge._editor_safe_runtime then return end
    assert(not bridge.is_running(), 'Install the mcp-tools runtime guard before starting its bridge')
    bridge._editor_safe_runtime = true
    local source = debug.getinfo(bridge.start, 'S').source:sub(2)
    local root = vim.fs.dirname(vim.fs.dirname(vim.fs.dirname(source)))
    local script = root .. '/bridge/src/index.ts'
    local stopping = false

    bridge.start = function(opts)
        if bridge.is_running() then return end
        local command
        if vim.fn.executable('bun') == 1 then
            command = { 'bun', 'run', script }
        else
            local loader = root .. '/bridge/node_modules/tsx/dist/loader.mjs'
            assert(vim.fn.executable('node') == 1 and vim.fn.filereadable(loader) == 1,
                'mcp-tools requires Bun or Node.js 18.19+ and npm install in its bridge directory')
            command = { 'node', '--import', vim.uri_from_fname(loader), script }
        end
        local random = assert(vim.uv.random(32))
        bridge._auth_token = random:gsub('.', function(c) return string.format('%02x', c:byte()) end)
        bridge._port = nil
        bridge._on_ready, bridge._on_stop = opts.on_ready, opts.on_stop
        bridge._stderr = {}
        local config = require('mcp-tools.config')
        local buffer, process = '', nil
        process = vim.system(command, {
            env = {
                NVIM_LISTEN_ADDRESS = opts.nvim_socket,
                MCP_PORT = tostring(opts.port or 0),
                MCP_AUTH_TOKEN = bridge._auth_token,
                MCP_LOG_FILE = config.get('bridge.log_file') or '',
                MCP_TOOL_TIMEOUT = tostring(config.get('bridge.tool_timeout') or 0),
            },
            stdout = function(_, data)
                if not data then return end
                buffer = (buffer .. data):sub(-4096)
                local port = buffer:match('MCP server listening on port (%d+)')
                if port then
                    vim.schedule(function()
                        if bridge._process ~= process or stopping or bridge._port then return end
                        bridge._port = tonumber(port)
                        if opts.on_ready then opts.on_ready(bridge._port) end
                    end)
                end
            end,
            stderr = function(_, data)
                if not data then return end
                vim.schedule(function()
                    if bridge._process ~= process then return end
                    table.insert(bridge._stderr, data)
                    if #bridge._stderr > 100 then table.remove(bridge._stderr, 1) end
                    if config.get('debug') then vim.notify('[mcp-tools] ' .. vim.trim(data), vim.log.levels.DEBUG) end
                end)
            end,
        }, function(result)
            vim.schedule(function()
                if bridge._process ~= process then return end
                bridge._process, bridge._port, bridge._auth_token = nil, nil, nil
                local expected = stopping
                stopping = false
                if not expected then
                    vim.notify('[mcp-tools] Bridge exited (' .. tostring(result.code) .. '); use :MCPToolsStart', vim.log.levels.WARN)
                end
                if opts.on_stop then opts.on_stop() end
            end)
        end)
        bridge._process = process
    end

    bridge.stop = function()
        local process = bridge._process
        if not process then return end
        stopping = true
        bridge._port = nil
        process:kill('sigkill')
        assert(vim.wait(3000, function() return bridge._process ~= process end, 20),
            'Timed out stopping mcp-tools; wait before restarting')
    end

    local group = vim.api.nvim_create_augroup('MCPToolsRuntime', { clear = true })
    vim.api.nvim_create_autocmd('VimLeavePre', { group = group, callback = function() bridge.stop() end })
    for name, callback in pairs({
        MCPToolsStart = function() require('mcp-tools').start() end,
        MCPToolsStop = function() require('mcp-tools').stop() end,
        MCPToolsRestart = function()
            require('mcp-tools').stop()
            require('mcp-tools').start()
        end,
        MCPToolsStatus = function()
            vim.notify(vim.inspect({ running = bridge.is_running(), port = bridge.get_port(), stderr = bridge._stderr }))
        end,
    }) do
        vim.api.nvim_create_user_command(name, callback, {})
    end
end

return M
