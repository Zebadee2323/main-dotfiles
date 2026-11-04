local M = {}
local actions = require("naia.ui.actions")
local approval = require("naia.ui.approval")
local policies = { companion = "allow", quickfix = "allow", navigation = "allow", card = "allow" }
-- User-owned startup defaults; session changes through :NaiaUIPolicy stay local.
local configured_policies = vim.g.naia_ui_policies
if configured_policies ~= nil then
  assert(type(configured_policies) == "table", "vim.g.naia_ui_policies must be a table")
  for kind, policy in pairs(configured_policies) do
    assert(policies[kind], "Unknown Naia UI capability: " .. tostring(kind))
    assert(vim.tbl_contains({ "ask", "allow", "off" }, policy), "Naia UI policy must be ask, allow, or off")
    policies[kind] = policy
  end
end
local requests, order, counter = {}, {}, 0
local selected

local function public(request)
  return {
    request_id = request.id, capability = request.kind, status = request.status,
    summary = request.summary, result = request.result,
  }
end

local function pending(id)
  local request = requests[id or selected]
  assert(request and request.status == "pending", "No matching pending UI request")
  return request
end

local function stop_timer(request)
  if request.timer and not request.timer:is_closing() then
    request.timer:stop()
    request.timer:close()
  end
  request.timer = nil
end

local present
local function finish(request, status, result)
  stop_timer(request)
  request.status, request.result = status, result
  -- Finished records retain outcomes, not potentially large source/content data.
  request.data, request.preview = nil, nil
  if selected == request.id then
    selected = nil
    approval.close()
  end
  vim.schedule(function() present() end)
end

local function apply(request)
  if request.status ~= "pending" then return end
  local ok, result = pcall(actions.apply, request.kind, request.data, function(target)
    M.request("navigation", target)
  end)
  finish(request, ok and "applied" or "failed", result)
  if not ok then
    local first_line = tostring(result):match("[^\n]+") or tostring(result)
    vim.notify("Naia " .. request.kind .. " failed: " .. first_line, vim.log.levels.WARN)
  end
end

function M.approve(id)
  local request = pending(id)
  -- Close the approval card before picking a target editing window.
  if selected == request.id then approval.close() end
  apply(request)
  return public(request)
end

function M.reject(id)
  local request = pending(id)
  finish(request, "rejected", "User declined the request")
end

function M.set_policy(kind, policy)
  assert(policies[kind], "Capability must be companion, quickfix, navigation, or card")
  assert(vim.tbl_contains({ "ask", "allow", "off" }, policy), "Policy must be ask, allow, or off")
  policies[kind] = policy
  if policy == "off" then
    if kind == "card" then actions.close_card() end
    for _, id in ipairs(order) do
      local request = requests[id]
      if request and request.kind == kind and request.status == "pending" then
        finish(request, "muted", "Capability disabled for this Neovim session")
      end
    end
  end
end

present = function()
  local request = selected and requests[selected]
  if not request or request.status ~= "pending" then
    selected, request = nil, nil
    for _, id in ipairs(order) do
      if requests[id] and requests[id].status == "pending" then
        selected, request = id, requests[id]
        break
      end
    end
  end
  if not request then approval.close(); return end
  approval.show(request, {
    approve = function() M.approve(request.id) end,
    reject = function() M.reject(request.id) end,
    allow = function() M.set_policy(request.kind, "allow"); M.approve(request.id) end,
    mute = function() M.set_policy(request.kind, "off") end,
  })
end

function M.request(kind, args)
  assert(policies[kind], "Unknown UI capability")
  if policies[kind] == "off" then
    return { capability = kind, status = "muted", result = "Disabled by the user; do not retry or bypass" }
  end
  local count = 0
  for _, request in pairs(requests) do
    if request.status == "pending" then count = count + 1 end
  end
  assert(count < 10, "Too many pending UI requests; wait for the user to review them")
  local data, summary, preview, reason = actions.prepare(kind, vim.deepcopy(args))
  counter = counter + 1
  local request = {
    id = "naia-ui-" .. counter, kind = kind, data = data, summary = summary,
    preview = preview, reason = reason, status = "pending",
  }
  requests[request.id] = request
  order[#order + 1] = request.id
  -- Bound history without ever dropping outstanding approvals.
  if #order > 100 then
    for index, id in ipairs(order) do
      if requests[id].status ~= "pending" then
        requests[id] = nil
        table.remove(order, index)
        break
      end
    end
  end
  local policy = policies[kind]
  if policy == "ask" then
    request.timer = vim.defer_fn(function()
      if request.status == "pending" then finish(request, "expired", "Approval expired after two minutes") end
    end, 120000)
  end
  vim.schedule(function()
    if request.status ~= "pending" then return end
    if policies[kind] == "off" then
      finish(request, "muted", "Capability disabled before application")
    elseif policy == "allow" and policies[kind] == "allow" then
      apply(request)
    else
      if not request.timer then
        request.timer = vim.defer_fn(function()
          if request.status == "pending" then finish(request, "expired", "Approval expired after two minutes") end
        end, 120000)
      end
      present()
    end
  end)
  local result = public(request)
  result.message = policy == "ask" and "Pending user approval; no editor changes applied. Do not poll repeatedly."
    or "Allowed by session policy; queued for application."
  return result
end

function M.status(id)
  assert(requests[id], "Unknown or expired UI request ID")
  return public(requests[id])
end

local function command(name, callback, opts)
  pcall(vim.api.nvim_del_user_command, name)
  vim.api.nvim_create_user_command(name, callback, opts)
end

command("NaiaApprove", function(opts) M.approve(opts.args ~= "" and opts.args or nil) end,
  { nargs = "?", desc = "Approve one pending Naia UI request" })
command("NaiaReject", function(opts) M.reject(opts.args ~= "" and opts.args or nil) end,
  { nargs = "?", desc = "Reject one pending Naia UI request" })
pcall(vim.api.nvim_del_user_command, "NaiaReview")
command("NaiaUIPolicy", function(opts)
  local kind, policy = opts.fargs[1], opts.fargs[2]
  assert(#opts.fargs <= 2, "Use :NaiaUIPolicy [companion|quickfix|navigation|card] [ask|allow|off]")
  if policy then M.set_policy(kind, policy)
  elseif kind then assert(policies[kind], "Unknown capability") end
  vim.notify(kind and (kind .. ": " .. policies[kind]) or vim.inspect(policies), vim.log.levels.INFO)
end, {
  nargs = "*", desc = "Set independent Naia UI permissions for this session",
  complete = function(_, line)
    return #vim.split(line, "%s+") <= 2 and { "companion", "quickfix", "navigation", "card" } or { "ask", "allow", "off" }
  end,
})
command("NaiaCompanion", function() actions.open_companion(true) end,
  { desc = "Open/focus the Naia companion buffer" })
command("NaiaCompanionPrevious", function() actions.cycle_companion(-1) end,
  { desc = "Show the previous companion buffer" })
command("NaiaCompanionNext", function() actions.cycle_companion(1) end,
  { desc = "Show the next companion buffer" })
command("NaiaCard", actions.toggle_card, { desc = "Toggle the selected pinned Naia card" })
command("NaiaCardClose", actions.close_card, { desc = "Dismiss the current pinned Naia card" })
command("NaiaCardPrevious", function() actions.cycle_card(-1) end,
  { desc = "Show the previous pinned card" })
command("NaiaCardNext", function() actions.cycle_card(1) end,
  { desc = "Show the next pinned card" })

vim.api.nvim_create_autocmd("VimLeavePre", {
  group = vim.api.nvim_create_augroup("NaiaUIRequests", { clear = true }),
  callback = function()
    for _, request in pairs(requests) do stop_timer(request) end
    approval.close()
    actions.close_card()
  end,
})

return M
