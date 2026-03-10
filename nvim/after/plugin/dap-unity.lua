-- nvim-dap + Unity (VSTU Code) attach helper
-- Drop this in (for example) lua/dap/unity.lua and require it from your dap setup file

local dap = require("dap")

-- ---------- small utils ----------
local uv = vim.loop

local function read_json(path)
    if vim.fn.filereadable(path) ~= 1 then return nil end
    local ok, lines = pcall(vim.fn.readfile, path)
    if not ok then return nil end
    local ok2, obj = pcall(vim.json.decode, table.concat(lines, "\n"))
    if not ok2 then return nil end
    return obj
end

local function sys()
    local u = uv.os_uname()
    return (u.sysname or ""):lower()
end

local function is_windows()
    return sys():find("windows") ~= nil or package.config:sub(1, 1) == "\\"
end

local function trim(s) return (s or ""):gsub("^%s+", ""):gsub("%s+$", "") end

local function system_lines(cmd, opts)
    -- Neovim 0.10: vim.system; older: vim.fn.systemlist
    opts = opts or {}
    local ok, res
    if vim.system then
        ok, res = pcall(function()
            local out = vim.system(cmd, { text = true }):wait()
            if out.code ~= 0 then return {} end
            local t = {}
            for line in (out.stdout or ""):gmatch("([^\r\n]+)") do table.insert(t, line) end
            return t
        end)
    else
        ok, res = pcall(function() return vim.fn.systemlist(table.concat(cmd, " ")) end)
    end
    return ok and res or {}
end

-- ---------- find VSTU Code adapter ----------
local function latest_vstuc_bin()
    local patterns = {}
    if is_windows() then
        -- VS Code extensions under %USERPROFILE%\.vscode\extensions
        table.insert(patterns, vim.fn.expand("~/AppData/Local/Programs/Microsoft VS Code/resources/app/extensions")) -- rarely used
        table.insert(patterns, vim.fn.expand("~/\\.vscode/extensions/visualstudiotoolsforunity.vstuc-*/bin"))
    else
        table.insert(patterns, "~/.vscode/extensions/visualstudiotoolsforunity.vstuc-*/bin")
    end
    local gl = {}
    for _, pat in ipairs(patterns) do
        local p = vim.fn.glob(pat, true, true)
        if type(p) == "table" then
            for _, x in ipairs(p) do table.insert(gl, x) end
        elseif type(p) == "string" and p ~= "" then
            table.insert(gl, p)
        end
    end
    if #gl == 0 then return nil end
    table.sort(gl)
    return gl[#gl]
end

-- ---------- find Unity root ----------
local function find_unity_root(startpath)
    local start = startpath
    if not start or start == "" then
        start = vim.api.nvim_buf_get_name(0)
        if not start or start == "" then start = uv.cwd() end
    end
    local function is_root(p)
        return vim.fn.isdirectory(p .. "/Assets") == 1 and vim.fn.isdirectory(p .. "/Library") == 1
    end
    if is_root(start) then return start end
    for p in vim.fs.parents(start) do
        if is_root(p) then return p end
    end
    return nil
end

-- ---------- read EditorInstance.json (PID etc.) ----------
local function read_editor_instance(root)
    local path = root .. "/Library/EditorInstance.json"
    local obj = read_json(path) or {}
    local pid = obj.process_id or obj.processId
    local log = obj.log_file or obj.logFile
    return { processId = pid, logFile = log }
end

-- ---------- process discovery (PID) ----------
local function find_unity_pids(root)
    -- Prefer the PID from EditorInstance.json if present
    local info = read_editor_instance(root)
    local pids = {}
    if info.processId then table.insert(pids, tonumber(info.processId)) end

    -- Fallback: scan processes by name
    if #pids == 0 then
        if is_windows() then
            -- Windows: use WMIC (legacy) or PowerShell Get-Process
            local lines = system_lines({ "powershell", "-NoProfile", "-Command",
            "Get-Process | Where-Object { $_.ProcessName -match 'Unity' } | Select-Object -ExpandProperty Id" })
            for _, l in ipairs(lines) do
                local n = tonumber(trim(l)); if n then table.insert(pids, n) end
            end
        else
            -- macOS/Linux
            local lines = system_lines({ "ps", "ax", "-o", "pid=,comm=" })
            for _, l in ipairs(lines) do
                local pid, comm = l:match("^%s*(%d+)%s+(.+)$")
                if pid and comm and (comm:find("Unity$", 1, true) or comm:find("Unity.app") or comm:find("Unity%a* Editor")) then
                    table.insert(pids, tonumber(pid))
                end
            end
        end
    end
    return pids
end

-- ---------- list listening TCP ports for a PID ----------
local function list_listen_ports_for_pid(pid)
    if not pid then return {} end
    local ports = {}
    if is_windows() then
        -- Prefer PowerShell Get-NetTCPConnection
        local ps = {
            "powershell", "-NoProfile", "-Command",
            ("Get-NetTCPConnection -State Listen -OwningProcess %d | Select-Object -ExpandProperty LocalPort"):format(
                pid)
            }
            local lines = system_lines(ps)
            if #lines == 0 then
                -- Fallback to netstat
                local ns = system_lines({ "cmd.exe", "/c", "netstat -ano | findstr LISTENING" })
                for _, l in ipairs(ns) do
                    local addr, state, owner = l:match("TCP%s+([%d%.:]+)%s+[%d%.:]+%s+LISTENING%s+(%d+)")
                    if addr and owner and tonumber(owner) == pid then
                        local port = tonumber(addr:match(":(%d+)$") or "")
                        if port then table.insert(ports, port) end
                    end
                end
            else
                for _, l in ipairs(lines) do
                    local n = tonumber(trim(l)); if n then table.insert(ports, n) end
                end
            end
        else
            -- macOS/Linux with lsof
            local lines = system_lines({ "lsof", "-Pan", "-p", tostring(pid), "-iTCP", "-sTCP:LISTEN" })
            for _, l in ipairs(lines) do
                -- Example: dotnet  12345 user   11u  IPv4 0x...  TCP 127.0.0.1:56380 (LISTEN)
                local port = l:match(":%s*(%d+)%s*%(%s*LISTEN%)") or l:match(":(%d+)%s*%(%s*LISTEN%)")
                if not port then port = l:match(":(%d+)%s+%(LISTEN%)") end
                if not port then
                    -- More tolerant: grab the last colon-number before (LISTEN)
                    port = l:match(":(%d+)%s*%b()")
                end
                port = tonumber(port or "")
                if port then table.insert(ports, port) end
            end
        end
        return ports
    end

    -- ---------- choose likely Unity debugger port ----------
    local function pick_debug_port(ports)
        -- Unity soft debugger generally lands in high 56xxx (varies by version).
        local MIN, MAX = 56000, 59000
        local candidates, others = {}, {}
        for _, p in ipairs(ports or {}) do
            if p >= MIN and p <= MAX then
                table.insert(candidates, p)
            else
                table.insert(others, p)
            end
        end
        table.sort(candidates)
        table.sort(others)
        return candidates[#candidates] or others[#others] -- prefer latest/highest
    end

    -- ---------- compute endpoint without parsing Editor.log ----------
    local function detect_unity_endpoint(root)
        root = root or find_unity_root()
        if not root then return nil end

        -- If user already knows the PID, try it first
        local pids = find_unity_pids(root)
        for _, pid in ipairs(pids) do
            local ports = list_listen_ports_for_pid(pid)
            local port = pick_debug_port(ports)
            if port then
                -- mono soft debugger usually binds loopback; vstuc accepts "127.0.0.1:PORT"
                return ("127.0.0.1:%d"):format(port)
            end
        end

        -- Last-ditch: scan a small port window (fast)
        local socket = require("vim.loop").new_tcp()
        local function can_connect(port)
            return pcall(function()
                local ok = socket:connect("127.0.0.1", port)
                -- uv tcp:connect is async; emulate a quick attempt by trying start_read then shutdown
                -- If connect throws, pcall returns false. If it doesn't, we assume reachable.
                socket:shutdown()
                socket:close()
                return ok
            end)
        end
        for port = 59000, 56000, -1 do
            if can_connect(port) then
                return ("127.0.0.1:%d"):format(port)
            end
        end

        return nil
    end

    -- ---------- DAP adapter ----------
    local vstuc_bin = latest_vstuc_bin()
    if not vstuc_bin then
        vim.notify(
            "VSTU Code adapter not found (visualstudiotoolsforunity.vstuc-*/bin). Make sure the VS Code 'VSTU Code' extension is installed.",
            vim.log.levels.ERROR
        )
    end

    dap.adapters.vstuc = {
        type = "executable",
        command = "dotnet",
        args = { (vstuc_bin or "") .. "/UnityDebugAdapter.dll" },
    }

    -- ---------- DAP config ----------
    dap.configurations.cs = {
        {
            type = "vstuc",
            request = "attach",
            name = "Attach to Unity (Editor/Player)",

            projectPath = function()
                return find_unity_root(vim.api.nvim_buf_get_name(0)) or uv.cwd()
            end,

            -- Prefer Editor PID for Unity 6+/modern editors
            processId = function()
                local root = find_unity_root(vim.api.nvim_buf_get_name(0)) or uv.cwd()
                local info = read_editor_instance(root)
                return info.processId -- may be nil; that's fine
            end,

            -- Player / older editors: infer endpoint without Editor.log
            endPoint = function()
                local root = find_unity_root(vim.api.nvim_buf_get_name(0)) or uv.cwd()
                local ep = detect_unity_endpoint(root)
                if not ep then
                    -- Optional: show a hint once so users know what failed
                    vim.schedule(function()
                        vim.notify(
                            "Unity debugger endpoint could not be inferred. Is the player/editor started with Script Debugging enabled?",
                            vim.log.levels.WARN)
                        end)
                    end
                    return ep
                end,

                cwd = function()
                    return find_unity_root(vim.api.nvim_buf_get_name(0)) or vim.loop.cwd()
                end,

                -- logFile = vim.fn.stdpath("cache") .. "/unity-vstuc.log",
                -- trace   = true,
            },
        }

        dap.listeners.after["event_initialized"]["vstuc_path_fix"] = function(session, _)
            if session.config.type ~= "vstuc" then return end

            local original_request = session.request
            local cwd = session.config.cwd or uv.cwd()

            local function canonicalize(p)
                if not p or p == "" then return p end

                -- Only touch relative/./ paths; leave absolute ones alone
                if p:sub(1, 2) == "./" then
                    p = cwd .. "/" .. p:sub(3)
                elseif not p:match("^/") and not p:match("^%a:[/\\]") then
                    -- bare relative like "Packages/..."
                    p = cwd .. "/" .. p
                end

                local rp = uv.fs_realpath(p)
                return rp or vim.fn.fnamemodify(p, ":p")
            end

            local function fix_response_paths(response)
                if not response or not response.stackFrames then return end
                for _, frame in ipairs(response.stackFrames) do
                    local source = frame.source
                    if source and type(source.path) == "string" then
                        source.path = canonicalize(source.path)
                    end
                end
            end

            session.request = function(self, command, arguments, callback)
                if command ~= "stackTrace" then
                    return original_request(self, command, arguments, callback)
                end

                if callback then
                    local wrapped = function(err, response)
                        fix_response_paths(response)
                        callback(err, response)
                    end
                    return original_request(self, command, arguments, wrapped)
                else
                    local err, response = original_request(self, command, arguments)
                    fix_response_paths(response)
                    return err, response
        end
    end
end
