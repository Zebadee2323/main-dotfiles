-- Legacy chat/YAML decoding. Portable tour files use file.lua instead.
-- Normalization is supplied by playback, which owns path resolution and steps.
local M = {}
local function trim(value)
  return type(value) == "string" and vim.trim(value) or ""
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
