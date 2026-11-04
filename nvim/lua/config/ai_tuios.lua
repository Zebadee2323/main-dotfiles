local M = {}

local function notify(message, level)
  vim.notify(message, level or vim.log.levels.ERROR, { title = "AISend / tuios" })
end

local function run(session, command, args, callback)
  local argv = { "tuios", command, "--session", session, "--json" }
  vim.list_extend(argv, args or {})
  local ok, err = pcall(vim.system, argv, { text = true, timeout = 10000 }, function(result)
    vim.schedule(function()
      if result.code ~= 0 then
        notify("tuios " .. command .. " failed: " .. vim.trim(result.stderr or result.stdout or ""))
        return
      end
      local decoded, data = pcall(vim.json.decode, result.stdout)
      if not decoded or type(data) ~= "table" then
        notify("tuios " .. command .. " returned invalid JSON")
      elseif data.success == false then
        notify(data.message or ("tuios " .. command .. " failed"))
      else
        callback(data)
      end
    end)
  end)
  if not ok then
    notify("Could not run tuios: " .. tostring(err))
  end
end

-- Return false only outside tuios, so failures never route to an unintended CLI.
function M.send(message)
  if vim.env.TUIOS_ENV ~= "1" then
    return false
  end

  local pane = vim.env.TUIOS_PANE_ID or vim.env.TUIOS_WINDOW_ID
  local session = vim.env.TUIOS_SESSION
  if not pane or pane == "" or not session or session == "" then
    notify("Missing TUIOS_PANE_ID / TUIOS_SESSION; cannot identify this editor's workspace")
    return true
  end
  if vim.fn.executable("tuios") ~= 1 then
    notify("tuios executable not found")
    return true
  end

  run(session, "list-windows", {}, function(data)
    local workspace
    local windows = {}
    for _, window in ipairs(data.windows or {}) do
      windows[window.window_id] = window
      if window.window_id == pane then
        workspace = window.workspace
      end
    end
    if workspace == nil then
      notify("Could not find this editor's tuios workspace")
      return
    end

    run(session, "list-agents", {}, function(agents_data)
      local agents = {}
      for _, agent in ipairs(agents_data.agents or {}) do
        local window = windows[agent.window_id]
        if window and window.workspace == workspace and agent.window_id ~= pane then
          agents[#agents + 1] = agent
        end
      end

      local function send(agent)
        if not agent then
          return -- Picker cancelled.
        end
        -- Recheck after the picker: the agent may have started work or moved.
        run(session, "list-windows", {}, function(current)
          local editor_workspace, target_workspace
          for _, window in ipairs(current.windows or {}) do
            if window.window_id == pane then
              editor_workspace = window.workspace
            elseif window.window_id == agent.window_id then
              target_workspace = window.workspace
            end
          end
          if editor_workspace == nil or target_workspace ~= editor_workspace then
            notify("Agent is no longer in this editor's tuios workspace")
            return
          end
          run(session, "list-agents", {}, function(current_agents)
            for _, target in ipairs(current_agents.agents or {}) do
              if target.window_id == agent.window_id then
                if target.state ~= "idle" and target.state ~= "done" and target.state ~= "errored" then
                  notify("Cannot paste into agent in state " .. (target.state or "unknown"), vim.log.levels.WARN)
                  return
                end
                -- Send raw text, including embedded newlines, without an extra Enter.
                run(session, "send-text", { "--window", target.window_id, "--", message }, function()
                  notify("Inserted draft into " .. (target.name or target.window_id), vim.log.levels.INFO)
                  run(session, "focus-window", { "--", target.window_id }, function() end)
                end)
                return
              end
            end
            notify("Selected pane is no longer an agent")
          end)
        end)
      end

      if #agents == 0 then
        notify("No agents in this editor's tuios workspace", vim.log.levels.WARN)
      elseif #agents == 1 then
        send(agents[1])
      else
        vim.ui.select(agents, {
          prompt = "Send to tuios agent:",
          format_item = function(agent)
            return string.format("%s [%s] (%s)", agent.name or agent.window_id,
              agent.state or "unknown", agent.window_id:sub(1, 8))
          end,
        }, send)
      end
    end)
  end)
  return true
end

return M
