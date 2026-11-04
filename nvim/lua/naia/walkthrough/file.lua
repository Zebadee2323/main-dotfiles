-- Portable persistence. Only documented fields cross the file boundary.
local M = {}
local uv = vim.uv or vim.loop
local function fail(message) error(message, 0) end
local function object(properties, required)
  return { type = "object", properties = properties, required = required, additionalProperties = false }
end
local function array(items) return { type = "array", items = items, minItems = 1 } end
local str = { type = "string" }
local positive = { type = "integer", minimum = 1 }
local location = object({ path = str, line_start = positive, line_end = positive,
  label = str, revision = str, text = str,
  anchor = object({ snippet = str, symbol = str, before = str, after = str }),
}, { "path", "line_start", "line_end" })
local beat = vim.deepcopy(location)
beat.required = { "description" }
beat.properties.description = str
beat.properties.highlights = array(location)
beat.properties.comparison = object({ left = location, right = location, diff = { type = "boolean" } }, { "left", "right" })
local step = vim.deepcopy(beat)
step.required = { "path", "line_start", "line_end", "description" }
step.properties.beats = array(beat)
step.properties.id, step.properties.parent_id = positive, positive
M.schema = object({ version = { type = "integer", enum = { 3 } }, steps = array(step) }, { "version", "steps" })

function M.root(root)
  local base = vim.fs.normalize(vim.fn.fnamemodify(root or vim.fn.getcwd(), ":p"))
  if not root then
    local result = vim.system({ "git", "-C", base, "rev-parse", "--show-toplevel" }, { text = true }):wait()
    if result.code == 0 then base = vim.trim(result.stdout) end
  end
  local real = uv.fs_realpath(base)
  local stat = real and uv.fs_stat(real)
  if not stat or stat.type ~= "directory" then fail("Walkthrough root must be an existing directory") end
  return real
end

local function relative(path)
  if type(path) ~= "string" or path == "" or path:find("[%c\\:]") or path:sub(1, 1) == "/"
    or path:sub(1, 1) == "~" then fail("Walkthrough paths must be root-relative") end
  for part in path:gmatch("[^/]+") do
    if part == ".." or part == "." then fail("Walkthrough paths cannot contain . or ..") end
  end
  return path
end

local function contained(root, path)
  local rel = vim.fs.relpath(root, path)
  if not rel or rel == "" then fail("Walkthrough path is outside the root") end
  relative(rel)
  -- Check existing ancestors too: proposed/deleted files may not exist yet.
  local ancestor = path
  while not uv.fs_lstat(ancestor) do
    local parent = vim.fs.dirname(ancestor)
    if parent == ancestor then fail("Cannot resolve walkthrough path") end
    ancestor = parent
  end
  local real = uv.fs_realpath(ancestor)
  if not real or (real ~= root and not vim.fs.relpath(root, real)) then
    fail("Walkthrough symlink escapes the root")
  end
  if real ~= root then relative(vim.fs.relpath(root, real)) end
  return rel
end

local function canonical(path)
  local ancestor, suffix = path, ""
  while not uv.fs_lstat(ancestor) do
    suffix = "/" .. vim.fs.basename(ancestor) .. suffix
    local parent = vim.fs.dirname(ancestor)
    if parent == ancestor then fail("Cannot resolve walkthrough path") end
    ancestor = parent
  end
  local real = uv.fs_realpath(ancestor)
  if not real then fail("Cannot resolve walkthrough symlink") end
  return real .. suffix
end

local function transform(value, schema, root, exporting, key)
  if schema.type == "object" then
    if type(value) ~= "table" or vim.islist(value) then fail("Expected walkthrough object") end
    local result = {}
    for _, name in ipairs(schema.required or {}) do
      if value[name] == nil then fail("Missing walkthrough field: " .. name) end
    end
    for name, item in pairs(value) do
      local field = schema.properties[name]
      if field then result[name] = transform(item, field, root, exporting, name)
      elseif not exporting then fail("Unknown walkthrough field: " .. tostring(name)) end
    end
    return result
  elseif schema.type == "array" then
    if type(value) ~= "table" or not vim.islist(value) or #value == 0 then fail("Expected non-empty walkthrough array") end
    local result = {}
    for _, item in ipairs(value) do result[#result + 1] = transform(item, schema.items, root, exporting) end
    return result
  end
  local kind = schema.type == "integer" and "number" or schema.type
  if type(value) ~= kind then fail("Invalid walkthrough field: " .. tostring(key)) end
  if schema.type == "integer" and (value % 1 ~= 0 or (schema.minimum and value < schema.minimum)) then
    fail("Invalid walkthrough integer: " .. tostring(key))
  end
  if schema.enum and not vim.tbl_contains(schema.enum, value) then fail("Unsupported walkthrough version") end
  if key == "path" then
    if exporting then return contained(root, canonical(vim.fs.normalize(value))) end
    relative(value)
    local absolute = vim.fs.normalize(root .. "/" .. value)
    contained(root, absolute)
    return absolute
  end
  return value
end

function M.encode(data, root)
  root = M.root(root)
  -- The engine's resolved paths, not the caller's possibly cwd-relative spellings,
  -- are authoritative. Runtime fields are dropped by the schema projection.
  local copy = vim.deepcopy(data)
  local function paths(loc)
    loc.path = loc.resolved_path or loc.path
    for _, child in ipairs(loc.highlights or {}) do paths(child) end
    for _, child in ipairs(loc.beats or {}) do paths(child) end
    if loc.comparison then paths(loc.comparison.left); paths(loc.comparison.right) end
  end
  for _, entry in ipairs(copy.steps) do paths(entry) end
  copy.version = 3
  return vim.json.encode(transform(copy, M.schema, root, true))
end

function M.decode(text, root)
  local data = vim.json.decode(text)
  return transform(data, M.schema, M.root(root), false).steps
end

function M.spec()
  return {
    schema = M.schema,
    format = "UTF-8 JSON, version 3; no comment header",
    paths = "All location paths use forward slashes relative to the Git repository root (cwd outside Git), or the explicit root supplied when loading. Never use absolute paths, ~, . or .. components, or symlinks outside the root. The file's directory is NOT the path base.",
    rules = {
      "Ranges are 1-based and inclusive. line_end must be >= line_start and within the source/snapshot.",
      "Steps require non-empty description. Beats require description and inherit omitted path/line fields from their step. Highlights inherit omitted fields at runtime, but files specify full locations.",
      "IDs are optional positive integers, unique across steps. A parent_id requires an id and must reference an earlier step. Array order is playback order.",
      "anchor requires snippet or symbol. Snippets match exact complete lines; before/after disambiguate. Symbols require LSP or Tree-sitter. Stale/ambiguous anchors fail.",
      "revision reads a local Git snapshot; text embeds a read-only proposed snapshot. Prefer text OR revision. Neither applies edits.",
      "Highlights must target the focused file or a visible comparison pane. Comparison requires left and right; diff is optional.",
      "Only documented fields are allowed. Runtime handles, resolved paths, session IDs and playback state are not persisted.",
      "Narration, labels, snippets and snapshot text are verbatim user content: review them for private information before sharing.",
    },
    example = { version = 3, steps = { { id = 1, path = "src/main.lua", line_start = 1, line_end = 3,
      description = "This is the entry point.", beats = { { description = "Start here.", line_start = 1, line_end = 1 } } } } },
  }
end

return M
