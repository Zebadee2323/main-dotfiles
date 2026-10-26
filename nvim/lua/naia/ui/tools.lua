local ui = require("naia.ui")
local M = {}

local reason = { type = "string", minLength = 1, maxLength = 2000,
  description = "Briefly explain why this editor action helps the user." }
local path = { type = "string", minLength = 1,
  description = "Local file path, absolute or relative to Neovim's current working directory. No URLs." }
local line = { type = "integer", minimum = 1, description = "1-based source line; defaults to 1." }
local column = { type = "integer", minimum = 1, description = "1-based byte column; defaults to 1." }
local links = {
  type = "array", maxItems = 100,
  description = "Source targets for Enter-to-navigate links. For inline Markdown links, provide display_line and "
    .. "the exact label appearing on that line. Without display_line, append the link in a Sources section. "
    .. "Only these explicit targets are navigable; arbitrary Markdown URLs are never executed.",
  items = {
    type = "object", additionalProperties = false,
    properties = {
      label = { type = "string", minLength = 1 }, path = path, line = line, column = column,
      display_line = { type = "integer", minimum = 1, description = "1-based line in the supplied content containing label." },
    },
    required = { "label", "path" },
  },
}
local pending = " Auto-allowed by default; user policies may require approval or disable this capability. "
  .. "Returns a pending request ID immediately while the action is queued; the editor remains responsive. "
  .. "Do not claim success until get_ui_request_status reports applied. Do not poll repeatedly while waiting "
  .. "for approval. Respect muted/rejected requests; never bypass them with execute_neovim_command or change user policies."

local specs = {
  show_companion = {
    title = "Show Companion",
    description = "Request permission to create a new read-only, listed Markdown companion buffer without taking focus. "
      .. "Reuse the companion split; preserve earlier buffers and their source links for session history. "
      .. "Use for explanations, diagrams, evidence, and source links, e.g. [Queue](path.lua#L42) plus structured links. "
      .. "The user can follow links with Enter; source navigation has its own approval policy." .. pending,
    kind = "companion",
    properties = {
      title = { type = "string", minLength = 1, maxLength = 100 },
      content = { type = "string", minLength = 1, maxLength = 100000, description = "Markdown, up to 1000 lines." },
      links = links, reason = reason,
    },
    required = { "title", "content", "reason" },
  },
  show_card = {
    title = "Show Pinned Card",
    description = "Show/update one small, passive pinned Markdown float without stealing focus. "
      .. "Allowed without prompting by default; the user can mute it independently. Keep content within eight "
      .. "wrapped rows at 46 columns (including any appended source links), and do not spam updates. "
      .. "Retain the last eight cards for user-controlled history. Prefer short diagrams, snippets, or notes. "
      .. "Links still obey the navigation permission." .. pending,
    kind = "card",
    properties = {
      title = { type = "string", minLength = 1, maxLength = 60 },
      content = { type = "string", minLength = 1, maxLength = 1200 }, links = links, reason = reason,
    },
    required = { "title", "content", "reason" },
  },
  set_quickfix = {
    title = "Update Quickfix",
    description = "Request permission to create, append to, or replace a quickfix list. Default new preserves "
      .. "the previous list in quickfix history. Append/replace fail safely if the list changed while pending. "
      .. "Does not open quickfix unless requested, and never jumps to an entry." .. pending,
    kind = "quickfix",
    properties = {
      title = { type = "string", minLength = 1, maxLength = 100 }, reason = reason,
      mode = { type = "string", description = "One of: new, append, replace. Defaults to new." },
      open = { type = "boolean", description = "Open quickfix without taking focus when applied; defaults to false." },
      items = {
        type = "array", maxItems = 500,
        items = {
          type = "object", additionalProperties = false,
          properties = {
            path = path, line = line, column = column,
            text = { type = "string", minLength = 1, maxLength = 2000 },
            type = { type = "string", enum = { "E", "W", "I", "N" } },
          },
          required = { "path", "text" },
        },
      },
    },
    required = { "title", "items", "reason" },
  },
  jump_to_location = {
    title = "Jump to Source",
    description = "Request permission to navigate a normal editing window to a local file and location. "
      .. "Never force-discard modified buffers. Preserve jump-back navigation with Ctrl-O." .. pending,
    kind = "navigation", properties = { path = path, line = line, column = column, reason = reason },
    required = { "path", "reason" },
  },
}

function M.register(naia)
  for name, spec in pairs(specs) do
    pcall(naia.deregister_tool, name)
    local args = vim.deepcopy(spec.properties)
    for key, field in pairs(args) do field.required = vim.tbl_contains(spec.required, key) end
    local ok, err = naia.register_tool(name, {
      description = spec.description,
      args = args,
      callback = function(args) return vim.json.encode(ui.request(spec.kind, args)) end,
    })
    if not ok then vim.notify("Could not register Naia " .. name .. ": " .. tostring(err), vim.log.levels.WARN) end
  end
  pcall(naia.deregister_tool, "get_ui_request_status")
  local ok, err = naia.register_tool("get_ui_request_status", {
    description = "Read the outcome of a UI request by ID: pending, applied, rejected, muted, expired, or failed. "
      .. "Use on demand, not in a polling loop; a pending approval is not success.",
    args = { request_id = { type = "string", required = true, description = "Non-empty UI request ID returned by a display tool." } },
    callback = function(args) return vim.json.encode(ui.status(args.request_id)) end,
  })
  if not ok then vim.notify("Could not register Naia UI status tool: " .. tostring(err), vim.log.levels.WARN) end
end

return M
