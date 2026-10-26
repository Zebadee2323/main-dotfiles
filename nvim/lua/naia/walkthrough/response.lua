-- Legacy chat/YAML decoding. Portable tour files use file.lua instead.
-- Normalization is supplied by playback, which owns path resolution and steps.
local M = {}
local function trim(value)
  return type(value) == "string" and vim.trim(value) or ""
end

local function field(step, keys)
  for _, key in ipairs(keys) do if step[key] ~= nil then return step[key] end end
  return nil
end

local function description(value)
  if type(value) ~= "string" then return "" end
  local cleaned = {}
  for _, line in ipairs(vim.split(value:gsub("\r\n", "\n"), "\n", { plain = true, trimempty = false })) do
    local text = vim.trim(line)
    local lower = text:lower()
    if text ~= "" and not lower:match("^```") and not lower:match("^</?commentary>")
      and not lower:match("^</?tool") and not lower:match("^assistant%s+to=")
      and not lower:match("^commentary%s+to=") and not lower:match("^function[s]?%.[%w_]+")
      and not lower:match("^recipient_name%s*:") and not lower:match("^parameters%s*:")
      and not lower:match("^tool_uses%s*:") and not lower:match("^%*%*commentary%*%*") then
      cleaned[#cleaned + 1] = text
    end
  end
  return vim.trim(table.concat(cleaned, " "):gsub("%s+", " "):gsub("%s+([,.;:!?])", "%1"))
end

-- The legacy field aliases apply only to chat/imported responses, never the
-- strict MCP schema. Resolve paths without depending on playback state.
function M.normalize(decoded, resolve)
  if type(decoded) == "table" and not vim.islist(decoded) then
    decoded = decoded.steps or decoded.walkthrough or decoded.items
  end
  if type(decoded) ~= "table" or not vim.islist(decoded) then return nil end
  local steps = {}
  for _, step in ipairs(decoded) do
    if type(step) == "table" then
      local path = trim(field(step, { "path", "file", "file_path", "filePath" }))
      local first = tonumber(field(step, { "line_start", "lineStart", "start_line", "startLine", "line", "line_number", "lineNumber" }))
      local last = tonumber(field(step, { "line_end", "lineEnd", "end_line", "endLine", "line", "line_number", "lineNumber" }))
      local text = description(field(step, { "description", "walkthrough_description", "walkthrough", "text" }))
      local id = tonumber(field(step, { "id", "step_id", "stepId" }))
      local parent = tonumber(field(step, { "parent_id", "parentId", "parent_step_id", "parentStepId" }))
      local resolved = resolve(path)
      if path ~= "" and first and first >= 1 and last and last >= 1 and text ~= "" and resolved then
        local start = math.max(1, math.floor(math.min(first, last)))
        steps[#steps + 1] = {
          id = id and math.floor(id) or nil, parent_id = parent and math.floor(parent) or nil,
          path = path, resolved_path = resolved, line_start = start,
          line_end = math.max(start, math.floor(math.max(first, last))), description = text,
          anchor = vim.deepcopy(step.anchor), beats = vim.deepcopy(step.beats),
          highlights = vim.deepcopy(step.highlights), comparison = vim.deepcopy(step.comparison),
        }
      end
    end
  end
  return #steps > 0 and steps or nil
end

local function sanitize(candidate)
  local lines = vim.split(candidate, "\n", { plain = true, trimempty = false })
  local filtered = {}
  for _, line in ipairs(lines) do
    local text = vim.trim(line)
    if text ~= "# ai-walkthrough" and text ~= "#ai-walkthrough" then
      filtered[#filtered + 1] = line
    end
  end
  for index, line in ipairs(filtered) do
    local text = vim.trim(line)
    if text:match("^-%s+") or text:match("^steps%s*:%s*$") or text:match("^walkthrough%s*:%s*$") then
      filtered = vim.list_slice(filtered, index, #filtered)
      break
    end
  end
  return vim.trim(table.concat(filtered, "\n"))
end

local function scalar(value)
  value = trim(value)
  if value == "null" or value == "~" then return nil end
  if value == "true" then return true end
  if value == "false" then return false end
  local number = tonumber(value)
  if number ~= nil then return number end
  local quote = value:sub(1, 1)
  if (quote == '"' or quote == "'") and value:sub(-1) == quote and #value >= 2 then
    value = value:sub(2, -2)
  end
  value = value:gsub('\\"', '"'):gsub("\\'", "'"):gsub("\\n", "\n")
  return value
end

local function manual_yaml(candidate)
  if candidate == "" then return nil end
  local items, current, root_key = {}, nil, nil
  local multiline_key, multiline_indent
  for _, raw_line in ipairs(vim.split(candidate, "\n", { plain = true, trimempty = false })) do
    local line = raw_line:gsub("\r$", "")
    local indent = #(line:match("^(%s*)") or "")
    local text = vim.trim(line)
    if text == "" then
      if multiline_key and current then
        current[multiline_key] = trim((current[multiline_key] or "") .. "\n")
      end
    elseif text:sub(1, 1) == "#" then
      -- Ignore comments, including inside legacy multiline values.
    elseif multiline_key and current and indent > (multiline_indent or 0) then
      local chunk = line:sub((multiline_indent or 0) + 1)
      current[multiline_key] = current[multiline_key] == "" and chunk
        or (current[multiline_key] .. "\n" .. chunk)
    else
      multiline_key, multiline_indent = nil, nil
      if text == "steps:" or text == "walkthrough:" or text == "items:" then
        root_key = text:match("^([%w_]+)")
      else
        local is_item = text:match("^-%s+") ~= nil
        if is_item then
          current = {}
          items[#items + 1] = current
          text = vim.trim(text:sub(2))
        end
        local key, value = text:match("^([%w_]+)%s*:%s*(.-)%s*$")
        if key and (current or root_key) then
          if not current then current = {}; items[#items + 1] = current end
          if value == "|" or value == ">" then
            current[key] = ""
            multiline_key, multiline_indent = key, indent + (is_item and 2 or 0)
          else
            current[key] = scalar(value)
          end
        end
      end
    end
  end
  return #items > 0 and items or nil
end

function M.json(text)
  local candidate = sanitize(text)
  if candidate == "" then return nil end
  local ok, decoded = pcall(vim.json.decode, candidate)
  if ok then return decoded end
end

function M.parse(text, normalize)
  local candidate = sanitize(text)
  local has_yaml, yaml = pcall(require, "codecompanion.utils.yaml")
  if has_yaml then
    local ok, decoded = pcall(yaml.decode, candidate)
    if ok then
      local steps = normalize(decoded)
      if steps then return steps end
    end
  end
  local json_steps = normalize(M.json(candidate))
  if json_steps then return json_steps end
  local steps = normalize(manual_yaml(candidate))
  if steps then return steps end
  return nil, "No valid walkthrough YAML was found"
    .. (not has_yaml and ", and YAML tree-sitter is unavailable" or "")
end

return M
