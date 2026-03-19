if type(_G.ai_walkthrough_stop_current) == "function" then
  pcall(_G.ai_walkthrough_stop_current, {
    restore_display = false,
    restore_focus = false,
    close_created_window = false,
    stop_voice = false,
    notify = false,
  })
end

local uv = vim.uv or vim.loop
local islist = vim.islist or vim.tbl_islist
local walkthrough_augroup = vim.api.nvim_create_augroup("ai_walkthrough", { clear = true })
local walkthrough_highlight_ns = vim.api.nvim_create_namespace("ai_walkthrough_active_step")
local walkthrough_highlight_group = "AIWalkthroughStep"
local walkthrough_description_highlight_group = "AIWalkthroughDescription"
local walkthrough_description_border_group = "AIWalkthroughDescriptionBorder"
local walkthrough_temp_keymaps = {
  { lhs = "<C-Space>", rhs = "<Cmd>AIWalkToggle<CR>", desc = "Toggle AI walkthrough playback" },
  { lhs = "<C-h>", rhs = "<Cmd>AIWalkPrev<CR>", desc = "Replay previous AI walkthrough step" },
  { lhs = "<C-l>", rhs = "<Cmd>AIWalkNext<CR>", desc = "Advance AI walkthrough by one step" },
  { lhs = "<C-j>", rhs = "<Cmd>AIWalkWindow<CR>", desc = "Open AI walkthrough browser" },
  { lhs = "<C-k>", rhs = "<Cmd>AIWalkParent<CR>", desc = "Jump to parent AI walkthrough step" },
}
local saved_walkthrough_keymaps = {}

local playback_state = {
  active = false,
  steps = nil,
  index = 0,
  timer = nil,
  waiting_for_voice = false,
  voice_started = false,
  pause_after_step = false,
  paused_for_continue = false,
  auto_pause_at_index = nil,
  focus_state = nil,
  display_state = nil,
  display_window = nil,
  display_window_created = false,
  highlighted_buffer = nil,
  description_buffer = nil,
  description_window = nil,
  browser_buffer = nil,
  browser_window = nil,
}

local last_walkthrough = nil
local pending_walkthrough_request = nil
local next_walkthrough_step_id = 1
local find_step_index_by_id
local collect_descendant_step_ids
local pause_walkthrough
local render_walkthrough_browser_window

local function create_or_replace_user_command(name, fn, opts)
  pcall(vim.api.nvim_del_user_command, name)
  vim.api.nvim_create_user_command(name, fn, opts)
end

local function unregister_walkthrough_keymaps()
  for _, keymap in ipairs(walkthrough_temp_keymaps) do
    pcall(vim.keymap.del, "n", keymap.lhs)

    local saved = saved_walkthrough_keymaps[keymap.lhs]
    if saved then
      pcall(vim.fn.mapset, saved.mode or "n", false, saved)
      saved_walkthrough_keymaps[keymap.lhs] = nil
    end
  end
end

local function register_walkthrough_keymaps()
  for _, keymap in ipairs(walkthrough_temp_keymaps) do
    if not saved_walkthrough_keymaps[keymap.lhs] then
      local existing = vim.fn.maparg(keymap.lhs, "n", false, true)
      if type(existing) == "table" and not vim.tbl_isempty(existing) then
        saved_walkthrough_keymaps[keymap.lhs] = existing
      end
    end

    vim.keymap.set("n", keymap.lhs, keymap.rhs, {
      desc = keymap.desc,
      silent = true,
    })
  end
end

local function trim(value)
  if type(value) ~= "string" then
    return ""
  end

  return vim.trim(value)
end

local function get_post_voice_delay_ms()
  local delay_ms = tonumber(vim.g.ai_walkthrough_post_voice_delay_ms)
  if delay_ms ~= nil then
    return math.max(0, math.floor(delay_ms))
  end

  local delay_seconds = tonumber(vim.g.ai_walkthrough_post_voice_delay_seconds)
  if delay_seconds ~= nil then
    return math.max(0, math.floor(delay_seconds * 1000))
  end

  return 2000
end

local function set_walkthrough_highlight()
  vim.api.nvim_set_hl(0, walkthrough_highlight_group, {
    bg = "#16381e",
    fg = "NONE",
  })

  vim.api.nvim_set_hl(0, walkthrough_description_highlight_group, {
    bg = "#0f1720",
    fg = "#d7e3f4",
  })

  vim.api.nvim_set_hl(0, walkthrough_description_border_group, {
    bg = "#0f1720",
    fg = "#4f6b8a",
  })
end

local function normalize_path(path)
  path = vim.fn.fnamemodify(path, ":p")

  if vim.fs and vim.fs.normalize then
    return vim.fs.normalize(path)
  end

  return path
end

local function is_absolute_path(path)
  if type(path) ~= "string" or path == "" then
    return false
  end

  return path:sub(1, 1) == "/" or path:sub(1, 1) == "~" or path:match("^%a:[/\\]") ~= nil
end

local function get_git_root(path)
  local dir = nil

  if path then
    local absolute = vim.fn.fnamemodify(path, ":p")
    if vim.fn.isdirectory(absolute) == 1 then
      dir = absolute
    else
      dir = vim.fn.fnamemodify(absolute, ":h")
    end
  end

  local cmd = dir and ("git -C " .. vim.fn.shellescape(dir) .. " rev-parse --show-toplevel")
    or "git rev-parse --show-toplevel"

  local ok, lines = pcall(vim.fn.systemlist, cmd)
  if not ok or type(lines) ~= "table" or #lines == 0 then
    return nil
  end

  local root = lines[1]
  if root == nil or root == "" then
    return nil
  end

  return root
end

local function get_repo_relative_path()
  local bufname = vim.api.nvim_buf_get_name(0)
  if bufname == "" then
    return nil
  end

  local abs_buf = vim.fn.fnamemodify(bufname, ":p")
  local git_root = get_git_root(abs_buf)

  if git_root then
    local abs_root = vim.fn.fnamemodify(git_root, ":p"):gsub("[/\\]$", "")
    local rel = vim.fs.relpath(abs_root, abs_buf)

    if rel then
      if rel == "" then
        return "."
      end

      return rel
    end
  end

  local cwd_rel = vim.fn.fnamemodify(bufname, ":.")
  if cwd_rel ~= "" then
    return cwd_rel
  end

  return bufname
end

local function build_context_anchor(opts)
  local path = get_repo_relative_path()
  if not path then
    return nil
  end

  if opts.range and opts.line1 and opts.line2 and opts.line2 >= opts.line1 then
    if opts.line1 == opts.line2 then
      return string.format("@%s %d", path, opts.line1)
    end

    return string.format("@%s %d-%d", path, opts.line1, opts.line2)
  end

  local line = vim.api.nvim_win_get_cursor(0)[1]
  return string.format("@%s %d", path, line)
end

local function get_target_chat()
  local ok, codecompanion = pcall(require, "codecompanion")

  if not ok then
    return nil, "CodeCompanion is not available"
  end

  local current_ok, current_chat = pcall(codecompanion.buf_get_chat, 0)
  if current_ok and current_chat and not current_chat.hidden then
    return current_chat
  end

  local last_chat = codecompanion.last_chat and codecompanion.last_chat() or nil
  if last_chat and not last_chat.hidden then
    return last_chat
  end

  return nil, "No active CodeCompanion chat found"
end

local function send_message_to_chat(chat, msg)
  if chat.current_request then
    vim.notify("CodeCompanion chat is busy", vim.log.levels.WARN)
    return false
  end

  if not chat.ui:is_visible() then
    chat.ui:open()
  elseif chat.ui.winnr and vim.api.nvim_win_is_valid(chat.ui.winnr) then
    vim.api.nvim_set_current_win(chat.ui.winnr)
  end

  local message_lines = vim.split(msg, "\n", { plain = true, trimempty = false })
  local was_locked = not vim.bo[chat.bufnr].modifiable

  if was_locked and chat.ui and chat.ui.unlock_buf then
    chat.ui:unlock_buf()
  end

  local line_count = vim.api.nvim_buf_line_count(chat.bufnr)
  local last_line = vim.api.nvim_buf_get_lines(chat.bufnr, line_count - 1, line_count, false)[1] or ""

  local ok, err = pcall(function()
    if last_line == "" then
      vim.api.nvim_buf_set_lines(chat.bufnr, line_count - 1, line_count, false, message_lines)
    else
      vim.api.nvim_buf_set_lines(chat.bufnr, line_count, line_count, false, vim.list_extend({ "" }, message_lines))
    end
  end)

  if was_locked and chat.ui and chat.ui.lock_buf then
    chat.ui:lock_buf()
  end

  if not ok then
    vim.notify("AI walkthrough could not write to the chat buffer: " .. tostring(err), vim.log.levels.WARN)
    return false
  end

  if chat.ui:is_visible() then
    chat.ui:follow()
  end

  return true
end

local function build_walkthrough_message(opts)
  local query = trim(opts.args or "")
  local lines = {
    "You are creating a text editor walkthrough for the user's query.",
    "The user's name is Ollie",
    "Reply with valid YAML only.",
    "The response must include the exact identifier comment `# ai-walkthrough` immediately before the YAML array.",
    "Return a top-level YAML array of steps.",
    "Each step must include `path`, `line_start`, `line_end`, and `description`.",
    "`path` must point to a real file and should usually be repo-relative.",
    "`line_start` and `line_end` must be 1-based line numbers that define the inclusive range to highlight for that step.",
    "Keep ranges focused on the most relevant block, and avoid ranges that span an entire file unless the file is truly tiny.",
    "If a step only needs one line, set `line_start` and `line_end` to the same value.",
    "`description` must be natural narration that sounds good when spoken aloud.",
    "Order the steps so they form a clear walkthrough that directly answers the query.",
    "Example:",
    "```yaml",
    "# ai-walkthrough",
    "- path: after/plugin/code-companion.lua",
    "  line_start: 548",
    "  line_end: 560",
    "  description: Start at the AISend command, because this is where editor context gets appended into the active chat input.",
    "- path: after/plugin/code-companion.lua",
    "  line_start: 447",
    "  line_end: 470",
    "  description: Then look at send_to_codecompanion, which picks the target chat and adds the prepared message to the chat buffer.",
    "```",
  }

  vim.list_extend(lines, {
    "",
    "User query:",
    query,
  })

  return table.concat(lines, "\n")
end

local function build_enquiry_message(step, query)
  local lines = {
    "You are answering a user's follow-up question about a single step from an existing text editor walkthrough.",
    "The user's name is Ollie",
    "Reply with valid YAML only.",
    "The response must include the exact identifier comment `# ai-walkthrough` immediately before the YAML array.",
    "Return a top-level YAML array of additional steps.",
    "Each step must include `path`, `line_start`, `line_end`, and `description`.",
    "`path` must point to a real file and should usually be repo-relative.",
    "`line_start` and `line_end` must be 1-based line numbers that define the inclusive range to highlight for that step.",
    "Keep ranges focused on the most relevant block, and avoid ranges that span an entire file unless the file is truly tiny.",
    "If a step only needs one line, set `line_start` and `line_end` to the same value.",
    "`description` must be natural narration that sounds good when spoken aloud.",
    "Answer the user's follow-up question in relation to the current walkthrough step.",
    "Return only the new in-between steps that should be inserted immediately after the current step.",
    "Stay tightly scoped to the same part of the codebase and preserve a clear walkthrough order.",
    "Example:",
    "```yaml",
    "# ai-walkthrough",
    "- path: after/plugin/code-companion.lua",
    "  line_start: 548",
    "  line_end: 554",
    "  description: First focus on where the command collects the editor context before anything is sent to the chat.",
    "- path: after/plugin/code-companion.lua",
    "  line_start: 555",
    "  line_end: 560",
    "  description: Then look at the branch that appends that prepared context into the active chat input.",
    "```",
    "",
    "Current step:",
    string.format("path: %s", step.path),
    string.format("line_start: %d", step.line_start),
    string.format("line_end: %d", step.line_end),
    string.format("description: %s", step.description),
    "",
    "User query:",
    query,
  }

  return table.concat(lines, "\n")
end

local function build_instruction_message(step, instruction)
  local lines = {
    "You are helping with a user's instruction about a single step from an existing text editor walkthrough.",
    "The user's name is Ollie",
    "Do not reply with YAML unless the user explicitly asks for it.",
    "Use the current walkthrough step as the main context for your response.",
    "Stay tightly scoped to the same part of the codebase unless the instruction clearly requires adjacent context.",
    "",
    "Current step:",
    string.format("path: %s", step.path),
    string.format("line_start: %d", step.line_start),
    string.format("line_end: %d", step.line_end),
    string.format("description: %s", step.description),
    "",
    "User instruction:",
    instruction,
  }

  return table.concat(lines, "\n")
end

local function send_walkthrough_request(opts, start_opts)
  local chat, err = get_target_chat()
  if not chat then
    vim.notify(err, vim.log.levels.WARN)
    return
  end

  pending_walkthrough_request = {
    mode = "start",
    start_opts = vim.deepcopy(start_opts or {}),
  }

  if not send_message_to_chat(chat, build_walkthrough_message(opts)) then
    pending_walkthrough_request = nil
  end
end

local function request_walkthrough_enquiry(opts)
  if not playback_state.active or not playback_state.steps then
    vim.notify("No AI walkthrough is currently running", vim.log.levels.WARN)
    return
  end

  pause_walkthrough()

  local query = trim(opts.args or "")

  local current_step = playback_state.steps[playback_state.index]
  if not current_step then
    vim.notify("No current AI walkthrough step is available to thread from", vim.log.levels.WARN)
    return
  end

  local chat, err = get_target_chat()
  if not chat then
    vim.notify(err, vim.log.levels.WARN)
    return
  end

  pending_walkthrough_request = {
    mode = "enquire",
    parent_step_id = current_step.id,
  }

  if not send_message_to_chat(chat, build_enquiry_message(current_step, query)) then
    pending_walkthrough_request = nil
  end
end

local function request_walkthrough_instruction(opts)
  if not playback_state.active or not playback_state.steps then
    vim.notify("No AI walkthrough is currently running", vim.log.levels.WARN)
    return
  end

  pause_walkthrough()

  local instruction = trim(opts.args or "")

  local current_step = playback_state.steps[playback_state.index]
  if not current_step then
    vim.notify("No current AI walkthrough step is available to instruct against", vim.log.levels.WARN)
    return
  end

  local chat, err = get_target_chat()
  if not chat then
    vim.notify(err, vim.log.levels.WARN)
    return
  end

  send_message_to_chat(chat, build_instruction_message(current_step, instruction))
end

local function flatten_message_content(content)
  if type(content) == "table" then
    content = table.concat(vim.tbl_map(function(part)
      if type(part) == "string" then
        return part
      end

      if type(part) == "table" and type(part.text) == "string" then
        return part.text
      end

      return ""
    end, content), "\n")
  end

  if type(content) ~= "string" then
    return nil
  end

  content = content:gsub("\r\n", "\n")
  if trim(content) == "" then
    return nil
  end

  return content
end

local function get_latest_assistant_raw_response(chat)
  local ok, codecompanion_config = pcall(require, "codecompanion.config")
  if not ok then
    return nil
  end

  local llm_role = codecompanion_config.constants.LLM_ROLE

  for i = #chat.messages, 1, -1 do
    local message = chat.messages[i]

    if message.role == llm_role and not (message.tools and message.tools.calls) then
      local content = flatten_message_content(message.content)
      if content then
        return content
      end
    end
  end

  return nil
end

local function find_walkthrough_identifier_start(text)
  if type(text) ~= "string" then
    return nil
  end

  local lower = text:lower()
  return lower:find("# ai-walkthrough", 1, true) or lower:find("#ai-walkthrough", 1, true)
end

local function has_walkthrough_identifier(text)
  return find_walkthrough_identifier_start(text) ~= nil
end

local function collect_yaml_candidates(text)
  local candidates = {}
  local seen = {}

  local function add(candidate)
    if type(candidate) ~= "string" then
      return
    end

    candidate = vim.trim(candidate:gsub("\r\n", "\n"))
    if candidate == "" or seen[candidate] then
      return
    end

    seen[candidate] = true
    table.insert(candidates, candidate)
  end

  for index, fenced in ipairs(vim.split(text, "```", { plain = true })) do
    if index % 2 == 0 then
      local lines = vim.split(fenced, "\n", { plain = true, trimempty = false })
      if #lines > 1 then
        table.remove(lines, 1)
      end
      add(table.concat(lines, "\n"))
    end
  end

  local identifier_start = find_walkthrough_identifier_start(text)
  if identifier_start then
    add(text:sub(identifier_start))
  end

  add(text)

  return candidates
end

local function strip_identifier_comment(text)
  if type(text) ~= "string" then
    return text
  end

  local lines = vim.split(text, "\n", { plain = true, trimempty = false })
  local filtered = {}

  for _, line in ipairs(lines) do
    local trimmed = vim.trim(line)
    if trimmed ~= "# ai-walkthrough" and trimmed ~= "#ai-walkthrough" then
      table.insert(filtered, line)
    end
  end

  return table.concat(filtered, "\n")
end

local function sanitize_yaml_candidate(candidate)
  candidate = strip_identifier_comment(candidate)

  local lines = vim.split(candidate, "\n", { plain = true, trimempty = false })
  local start_index = nil

  for index, line in ipairs(lines) do
    local trimmed = vim.trim(line)
    if trimmed:match("^-%s+") or trimmed:match("^steps%s*:%s*$") or trimmed:match("^walkthrough%s*:%s*$") then
      start_index = index
      break
    end
  end

  if start_index then
    lines = vim.list_slice(lines, start_index, #lines)
  end

  return vim.trim(table.concat(lines, "\n"))
end

local function unquote_yaml_scalar(value)
  value = trim(value)
  local quote = value:sub(1, 1)

  if (quote == '"' or quote == "'") and value:sub(-1) == quote and #value >= 2 then
    value = value:sub(2, -2)
  end

  value = value:gsub('\\"', '"')
  value = value:gsub("\\'", "'")
  value = value:gsub("\\n", "\n")

  return value
end

local function parse_walkthrough_scalar(value)
  value = trim(value)
  if value == "" then
    return ""
  end

  if value == "null" or value == "~" then
    return nil
  end

  if value == "true" then
    return true
  end

  if value == "false" then
    return false
  end

  local number = tonumber(value)
  if number ~= nil then
    return number
  end

  return unquote_yaml_scalar(value)
end

local function parse_manual_walkthrough_yaml(candidate)
  candidate = sanitize_yaml_candidate(candidate)
  if candidate == "" then
    return nil
  end

  local lines = vim.split(candidate, "\n", { plain = true, trimempty = false })
  local root_key = nil
  local items = {}
  local current = nil
  local pending_multiline_key = nil
  local pending_multiline_indent = nil

  local function ensure_current()
    if not current then
      current = {}
      table.insert(items, current)
    end
    return current
  end

  local function finish_multiline()
    pending_multiline_key = nil
    pending_multiline_indent = nil
  end

  for _, raw_line in ipairs(lines) do
    local line = raw_line:gsub("\r$", "")
    local indent = #(line:match("^(%s*)") or "")
    local trimmed = vim.trim(line)

    if trimmed == "" then
      if pending_multiline_key and current then
        current[pending_multiline_key] = trim((current[pending_multiline_key] or "") .. "\n")
      end
    elseif trimmed:sub(1, 1) == "#" then
      -- ignore comment lines
    elseif pending_multiline_key and current and indent > (pending_multiline_indent or 0) then
      local chunk = line:sub((pending_multiline_indent or 0) + 1)
      current[pending_multiline_key] = current[pending_multiline_key] == "" and chunk
        or (current[pending_multiline_key] .. "\n" .. chunk)
    else
      finish_multiline()

      if trimmed:match("^[%w_]+%s*:%s*$") and (trimmed == "steps:" or trimmed == "walkthrough:" or trimmed == "items:") then
        root_key = trimmed:match("^([%w_]+)")
      elseif trimmed:match("^-%s+") then
        local remainder = vim.trim(trimmed:sub(2))
        current = {}
        table.insert(items, current)

        if remainder ~= "" then
          local key, value = remainder:match("^([%w_]+)%s*:%s*(.-)%s*$")
          if key then
            if value == "|" or value == ">" then
              current[key] = ""
              pending_multiline_key = key
              pending_multiline_indent = indent + 2
            else
              current[key] = parse_walkthrough_scalar(value)
            end
          end
        end
      else
        local key, value = trimmed:match("^([%w_]+)%s*:%s*(.-)%s*$")
        if key and (current or root_key) then
          ensure_current()
          if value == "|" or value == ">" then
            current[key] = ""
            pending_multiline_key = key
            pending_multiline_indent = indent
          else
            current[key] = parse_walkthrough_scalar(value)
          end
        end
      end
    end
  end

  finish_multiline()

  if #items == 0 then
    return nil
  end

  return items
end

local function parse_json_walkthrough(candidate)
  candidate = sanitize_yaml_candidate(candidate)
  if candidate == "" then
    return nil
  end

  local ok, decoded = pcall(vim.json.decode, candidate)
  if ok then
    return decoded
  end

  return nil
end

local function get_resolution_roots()
  local roots = {}
  local seen = {}

  local function add(path)
    if not path or path == "" then
      return
    end

    local normalized = normalize_path(path)
    if seen[normalized] then
      return
    end

    seen[normalized] = true
    table.insert(roots, normalized)
  end

  local cwd = vim.fn.getcwd()
  add(cwd)
  add(get_git_root(cwd))

  local bufname = vim.api.nvim_buf_get_name(0)
  if bufname ~= "" then
    add(vim.fn.fnamemodify(bufname, ":p:h"))
    add(get_git_root(bufname))
  end

  return roots
end

local function resolve_step_path(path)
  path = trim(path)
  if path == "" then
    return nil
  end

  if is_absolute_path(path) then
    local absolute = normalize_path(path)
    if vim.fn.filereadable(absolute) == 1 then
      return absolute
    end

    return nil
  end

  for _, root in ipairs(get_resolution_roots()) do
    local candidate = normalize_path(root .. "/" .. path)
    if vim.fn.filereadable(candidate) == 1 then
      return candidate
    end
  end

  return nil
end

local function get_table_value(step, keys)
  for _, key in ipairs(keys) do
    local value = step[key]
    if value ~= nil then
      return value
    end
  end

  return nil
end

local function sanitize_walkthrough_description(value)
  if type(value) ~= "string" then
    return ""
  end

  local lines = vim.split(value:gsub("\r\n", "\n"), "\n", { plain = true, trimempty = false })
  local cleaned = {}

  for _, line in ipairs(lines) do
    local trimmed = vim.trim(line)
    local lower = trimmed:lower()

    if trimmed ~= ""
        and trimmed ~= "```"
        and not lower:match("^```")
        and not lower:match("^<commentary>")
        and not lower:match("^</commentary>")
        and not lower:match("^<tool")
        and not lower:match("^</tool")
        and not lower:match("^assistant%s+to=")
        and not lower:match("^commentary%s+to=")
        and not lower:match("^function[s]?%.[%w_]+")
        and not lower:match("^recipient_name%s*:")
        and not lower:match("^parameters%s*:")
        and not lower:match("^tool_uses%s*:")
        and not lower:match("^%*%*commentary%*%*") then
      table.insert(cleaned, trimmed)
    end
  end

  local text = trim(table.concat(cleaned, " "))
  text = text:gsub("%s+", " ")
  text = text:gsub("%s+([,.;:!?])", "%1")
  text = trim(text)

  return text
end

local function assign_step_identity(step, parent_id)
  if type(step) ~= "table" then
    return step
  end

  if step.id == nil then
    step.id = next_walkthrough_step_id
    next_walkthrough_step_id = next_walkthrough_step_id + 1
  else
    next_walkthrough_step_id = math.max(next_walkthrough_step_id, tonumber(step.id) or 0)
    next_walkthrough_step_id = next_walkthrough_step_id + 1
  end

  if parent_id ~= nil then
    step.parent_id = parent_id
  elseif step.parent_id == nil then
    step.parent_id = nil
  end

  return step
end

local function prepare_walkthrough_steps(steps, parent_id)
  if type(steps) ~= "table" then
    return steps
  end

  local prepared = {}

  for _, step in ipairs(steps) do
    if type(step) == "table" then
      local copied = vim.deepcopy(step)
      assign_step_identity(copied, parent_id)
      table.insert(prepared, copied)
    end
  end

  return prepared
end

local function yaml_quote(value)
  value = tostring(value or "")
  value = value:gsub("\\", "\\\\")
  value = value:gsub('"', '\\"')
  value = value:gsub("\n", "\\n")
  return '"' .. value .. '"'
end

local function normalize_walkthrough_steps(decoded)
  if type(decoded) == "table" and not islist(decoded) then
    decoded = decoded.steps or decoded.walkthrough or decoded.items
  end

  if type(decoded) ~= "table" or not islist(decoded) then
    return nil
  end

  local steps = {}

  for _, step in ipairs(decoded) do
    if type(step) == "table" then
      local path = trim(get_table_value(step, { "path", "file", "file_path", "filePath" }))
      local line_start = tonumber(get_table_value(step, {
        "line_start",
        "lineStart",
        "start_line",
        "startLine",
        "line",
        "line_number",
        "lineNumber",
      }))
      local line_end = tonumber(get_table_value(step, {
        "line_end",
        "lineEnd",
        "end_line",
        "endLine",
        "line",
        "line_number",
        "lineNumber",
      }))
      local description = sanitize_walkthrough_description(get_table_value(step, {
        "description",
        "walkthrough_description",
        "walkthrough",
        "text",
      }))
      local step_id = tonumber(get_table_value(step, { "id", "step_id", "stepId" }))
      local parent_id = tonumber(get_table_value(step, { "parent_id", "parentId", "parent_step_id", "parentStepId" }))
      local resolved_path = resolve_step_path(path)

      if path ~= "" and line_start and line_start >= 1 and line_end and line_end >= 1 and description ~= "" and resolved_path then
        local normalized_line_start = math.max(1, math.floor(math.min(line_start, line_end)))
        local normalized_line_end = math.max(normalized_line_start, math.floor(math.max(line_start, line_end)))

        table.insert(steps, {
          id = step_id and math.floor(step_id) or nil,
          parent_id = parent_id and math.floor(parent_id) or nil,
          path = path,
          resolved_path = resolved_path,
          line_start = normalized_line_start,
          line_end = normalized_line_end,
          description = description,
        })
      end
    end
  end

  if #steps == 0 then
    return nil
  end

  return steps
end

local function parse_walkthrough_response(text)
  local ok, yaml = pcall(require, "codecompanion.utils.yaml")

  for _, candidate in ipairs(collect_yaml_candidates(text)) do
    local sanitized_candidate = sanitize_yaml_candidate(candidate)

    if ok then
      local decode_ok, decoded = pcall(yaml.decode, sanitized_candidate)
      if decode_ok then
        local steps = normalize_walkthrough_steps(decoded)
        if steps then
          return steps
        end
      end
    end

    do
      local json_decoded = parse_json_walkthrough(candidate)
      local steps = normalize_walkthrough_steps(json_decoded)
      if steps then
        return steps
      end
    end

    do
      local manual_decoded = parse_manual_walkthrough_yaml(candidate)
      local steps = normalize_walkthrough_steps(manual_decoded)
      if steps then
        return steps
      end
    end
  end

  if not ok then
    return nil, "No valid walkthrough YAML was found, and YAML tree-sitter is unavailable"
  end

  return nil, "No valid walkthrough YAML was found"
end

local function get_buffer_absolute_path(bufnr)
  local bufname = vim.api.nvim_buf_get_name(bufnr)
  if bufname == "" then
    return nil
  end

  return normalize_path(bufname)
end

local function is_chat_buffer(bufnr)
  local ok, codecompanion = pcall(require, "codecompanion")
  if not ok then
    return false
  end

  local chat_ok, chat = pcall(codecompanion.buf_get_chat, bufnr)
  return chat_ok and chat ~= nil
end

local function is_candidate_display_window(winid)
  if not (winid and vim.api.nvim_win_is_valid(winid)) then
    return false
  end

  local bufnr = vim.api.nvim_win_get_buf(winid)
  if not vim.api.nvim_buf_is_valid(bufnr) then
    return false
  end

  if vim.bo[bufnr].buftype ~= "" or is_chat_buffer(bufnr) then
    return false
  end

  return true
end

local function get_window_area(winid)
  if not (winid and vim.api.nvim_win_is_valid(winid)) then
    return 0
  end

  local width = vim.api.nvim_win_get_width(winid)
  local height = vim.api.nvim_win_get_height(winid)

  return math.max(width, 0) * math.max(height, 0)
end

local function find_best_display_window(target_path)
  local best_same_file = nil
  local best_unmodified = nil
  local best_any = nil
  local best_same_file_area = -1
  local best_unmodified_area = -1
  local best_any_area = -1

  for _, winid in ipairs(vim.api.nvim_tabpage_list_wins(0)) do
    if is_candidate_display_window(winid) then
      local bufnr = vim.api.nvim_win_get_buf(winid)
      local buf_path = get_buffer_absolute_path(bufnr)
      local area = get_window_area(winid)

      if buf_path == target_path and area > best_same_file_area then
        best_same_file = winid
        best_same_file_area = area
      end

      if not vim.bo[bufnr].modified and area > best_unmodified_area then
        best_unmodified = winid
        best_unmodified_area = area
      end

      if area > best_any_area then
        best_any = winid
        best_any_area = area
      end
    end
  end

  return best_same_file or best_unmodified or best_any
end

local function create_display_window(base_window)
  if not (base_window and vim.api.nvim_win_is_valid(base_window)) then
    return nil
  end

  local base_bufnr = vim.api.nvim_win_get_buf(base_window)
  local split_cmd = is_chat_buffer(base_bufnr) and "botright vnew" or "vsplit"

  pcall(vim.api.nvim_set_current_win, base_window)

  local ok = pcall(vim.cmd, split_cmd)
  if not ok then
    return nil
  end

  return vim.api.nvim_get_current_win()
end

local function select_display_window(target_path)
  local winid = find_best_display_window(target_path)
  if not winid then
    local created = create_display_window(vim.api.nvim_get_current_win())
    return created, created ~= nil
  end

  local bufnr = vim.api.nvim_win_get_buf(winid)
  local buf_path = get_buffer_absolute_path(bufnr)

  return winid, false
end

local function capture_window_state(winid)
  if not (winid and vim.api.nvim_win_is_valid(winid)) then
    return nil
  end

  local ok, view = pcall(vim.api.nvim_win_call, winid, function()
    return vim.fn.winsaveview()
  end)

  return {
    window_id = winid,
    buffer_id = vim.api.nvim_win_get_buf(winid),
    view = ok and view or nil,
  }
end

local function restore_window_state(state)
  if not (state and state.window_id and vim.api.nvim_win_is_valid(state.window_id)) then
    return false
  end

  if state.buffer_id and vim.api.nvim_buf_is_valid(state.buffer_id) then
    pcall(vim.api.nvim_win_set_buf, state.window_id, state.buffer_id)
  end

  if state.view then
    pcall(vim.api.nvim_win_call, state.window_id, function()
      vim.fn.winrestview(state.view)
    end)
  end

  return true
end

local function stop_timer()
  if playback_state.timer then
    pcall(playback_state.timer.stop, playback_state.timer)
    pcall(playback_state.timer.close, playback_state.timer)
    playback_state.timer = nil
  end
end

local function invoke_user_command(name, args)
  local ok = pcall(vim.api.nvim_cmd, {
    cmd = name,
    args = args or {},
  }, {})

  return ok
end

local function clear_active_step_highlight()
  if playback_state.highlighted_buffer and vim.api.nvim_buf_is_valid(playback_state.highlighted_buffer) then
    pcall(vim.api.nvim_buf_clear_namespace, playback_state.highlighted_buffer, walkthrough_highlight_ns, 0, -1)
  end

  playback_state.highlighted_buffer = nil
end

local function close_walkthrough_description_box()
  if playback_state.description_window and vim.api.nvim_win_is_valid(playback_state.description_window) then
    pcall(vim.api.nvim_win_close, playback_state.description_window, true)
  end

  if playback_state.description_buffer and vim.api.nvim_buf_is_valid(playback_state.description_buffer) then
    pcall(vim.api.nvim_buf_delete, playback_state.description_buffer, { force = true })
  end

  playback_state.description_window = nil
  playback_state.description_buffer = nil
end

local function close_walkthrough_browser_window()
  if playback_state.browser_window and vim.api.nvim_win_is_valid(playback_state.browser_window) then
    pcall(vim.api.nvim_win_close, playback_state.browser_window, true)
  end

  if playback_state.browser_buffer and vim.api.nvim_buf_is_valid(playback_state.browser_buffer) then
    pcall(vim.api.nvim_buf_delete, playback_state.browser_buffer, { force = true })
  end

  playback_state.browser_window = nil
  playback_state.browser_buffer = nil
end

local function focus_walkthrough_browser_window()
  if playback_state.browser_window and vim.api.nvim_win_is_valid(playback_state.browser_window) then
    pcall(vim.api.nvim_set_current_win, playback_state.browser_window)
    return true
  end

  return false
end

local function wrap_text(text, width)
  local lines = {}

  for _, paragraph in ipairs(vim.split(text, "\n", { plain = true, trimempty = false })) do
    local current = ""

    for word in paragraph:gmatch("%S+") do
      local candidate = current == "" and word or (current .. " " .. word)
      if vim.fn.strdisplaywidth(candidate) <= width then
        current = candidate
      else
        if current ~= "" then
          table.insert(lines, current)
        end

        if vim.fn.strdisplaywidth(word) <= width then
          current = word
        else
          local chunk = ""
          for _, char in ipairs(vim.split(word, "\\zs")) do
            local next_chunk = chunk .. char
            if vim.fn.strdisplaywidth(next_chunk) > width and chunk ~= "" then
              table.insert(lines, chunk)
              chunk = char
            else
              chunk = next_chunk
            end
          end
          current = chunk
        end
      end
    end

    if current ~= "" then
      table.insert(lines, current)
    elseif paragraph == "" then
      table.insert(lines, "")
    end
  end

  if #lines == 0 then
    return { "" }
  end

  return lines
end

local function get_window_relative_line_row(winid, line)
  if not (winid and vim.api.nvim_win_is_valid(winid)) then
    return nil
  end

  local ok, pos = pcall(vim.fn.screenpos, winid, line, 1)
  local win_pos = vim.fn.win_screenpos(winid)
  if ok and type(pos) == "table" and pos.row and pos.row > 0 and type(win_pos) == "table" and win_pos[1] then
    return math.max(0, pos.row - win_pos[1])
  end

  local view = vim.api.nvim_win_call(winid, function()
    return vim.fn.winsaveview()
  end)
  return math.max(0, line - (view.topline or 1))
end

local function build_walkthrough_description_lines(step_index, description, width)
  local total_steps = playback_state.steps and #playback_state.steps or 0
  local steps = playback_state.steps or {}
  local current_step = steps[step_index] or {}
  local parent_index = current_step.parent_id and find_step_index_by_id(steps, current_step.parent_id) or nil
  local lines = {
    string.format("Step %d/%d", step_index, total_steps),
  }

  if parent_index then
    local child_index = 0
    local child_total = 0

    for _, step in ipairs(steps) do
      if step.parent_id == current_step.parent_id then
        child_total = child_total + 1
        if step.id == current_step.id then
          child_index = child_total
        end
      end
    end

    table.insert(lines, string.format("Parent: Step %d", parent_index))
    table.insert(lines, string.format("Child Step %d/%d", child_index, child_total))
  end

  table.insert(lines, "")

  vim.list_extend(lines, wrap_text(description, width))
  return lines
end

local function show_walkthrough_description_box(winid, step_index, line_start, description)
  close_walkthrough_description_box()

  if not (winid and vim.api.nvim_win_is_valid(winid)) then
    return false
  end

  local width = vim.api.nvim_win_get_width(winid)
  local height = vim.api.nvim_win_get_height(winid)
  local max_inner_width = width - 4
  if max_inner_width < 16 or height < 3 then
    return false
  end

  local inner_width = math.min(math.max(24, math.floor(width * 0.35)), max_inner_width)
  if inner_width < 16 then
    return false
  end

  local wrapped = build_walkthrough_description_lines(step_index, description, inner_width)
  local box_height = math.min(#wrapped, math.max(1, height - 2))
  local display_lines = vim.list_slice(wrapped, 1, box_height)
  local start_row = get_window_relative_line_row(winid, line_start) or 0
  local outer_height = box_height + 2
  local row = math.max(0, math.min(start_row - outer_height, math.max(0, height - outer_height)))
  local col = math.max(0, width - inner_width - 2)

  local bufnr = vim.api.nvim_create_buf(false, true)
  if not bufnr then
    return false
  end

  vim.bo[bufnr].bufhidden = "wipe"
  vim.bo[bufnr].modifiable = true
  vim.api.nvim_buf_set_lines(bufnr, 0, -1, false, display_lines)
  vim.bo[bufnr].modifiable = false

  local ok_float, float_win = pcall(vim.api.nvim_open_win, bufnr, false, {
    relative = "win",
    win = winid,
    row = row,
    col = col,
    width = inner_width,
    height = box_height,
    style = "minimal",
    border = "rounded",
    focusable = false,
    noautocmd = true,
    zindex = 150,
  })

  if not ok_float then
    pcall(vim.api.nvim_buf_delete, bufnr, { force = true })
    return false
  end

  vim.wo[float_win].wrap = true
  vim.wo[float_win].winhl = table.concat({
    "Normal:" .. walkthrough_description_highlight_group,
    "NormalFloat:" .. walkthrough_description_highlight_group,
    "FloatBorder:" .. walkthrough_description_border_group,
  }, ",")

  playback_state.description_buffer = bufnr
  playback_state.description_window = float_win
  return true
end

local function highlight_walkthrough_step(bufnr, line_start, line_end)
  clear_active_step_highlight()

  if not (bufnr and vim.api.nvim_buf_is_valid(bufnr)) then
    return false
  end

  local line_count = math.max(1, vim.api.nvim_buf_line_count(bufnr))
  local start_row = math.min(math.max(line_start, 1), line_count) - 1
  local end_row = math.min(math.max(line_end, line_start), line_count)

  pcall(vim.api.nvim_buf_set_extmark, bufnr, walkthrough_highlight_ns, start_row, 0, {
    end_row = end_row,
    end_col = 0,
    hl_group = walkthrough_highlight_group,
    hl_eol = true,
    priority = 200,
    strict = false,
  })

  playback_state.highlighted_buffer = bufnr
  return true
end

local function focus_walkthrough_range(winid, line_start, line_end)
  if not (winid and vim.api.nvim_win_is_valid(winid)) then
    return false
  end

  local bufnr = vim.api.nvim_win_get_buf(winid)
  if not vim.api.nvim_buf_is_valid(bufnr) then
    return false
  end

  local line_count = math.max(1, vim.api.nvim_buf_line_count(bufnr))
  local normalized_start = math.min(math.max(line_start, 1), line_count)
  local normalized_end = math.min(math.max(line_end, normalized_start), line_count)
  local win_height = math.max(1, vim.api.nvim_win_get_height(winid))
  local range_height = normalized_end - normalized_start + 1
  local cursor_line = range_height <= win_height
      and math.floor((normalized_start + normalized_end) / 2)
    or normalized_start
  local topline = nil

  if range_height > win_height then
    topline = normalized_start
  else
    local padding = math.floor((win_height - range_height) / 2)
    local max_topline = math.max(1, line_count - win_height + 1)
    topline = math.min(math.max(normalized_start - padding, 1), max_topline)
  end

  vim.api.nvim_win_set_cursor(winid, { cursor_line, 0 })
  pcall(vim.api.nvim_win_call, winid, function()
    vim.fn.winrestview({ topline = topline })
  end)

  return true
end

local function reset_playback_state()
  clear_active_step_highlight()
  close_walkthrough_description_box()
  close_walkthrough_browser_window()
  unregister_walkthrough_keymaps()
  playback_state.active = false
  playback_state.steps = nil
  playback_state.index = 0
  playback_state.waiting_for_voice = false
  playback_state.voice_started = false
  playback_state.pause_after_step = false
  playback_state.paused_for_continue = false
  playback_state.auto_pause_at_index = nil
  playback_state.focus_state = nil
  playback_state.display_state = nil
  playback_state.display_window = nil
  playback_state.display_window_created = false
  playback_state.highlighted_buffer = nil
  playback_state.description_buffer = nil
  playback_state.description_window = nil
  playback_state.browser_buffer = nil
  playback_state.browser_window = nil
end

local function stop_walkthrough(opts)
  opts = opts or {}

  local had_active_walkthrough = playback_state.active or playback_state.timer ~= nil

  stop_timer()

  if opts.stop_voice and (had_active_walkthrough or opts.force_stop_voice) then
    invoke_user_command("AIVoiceStop")
  end

  if opts.restore_display then
    restore_window_state(playback_state.display_state)
  end

  if opts.close_created_window and playback_state.display_window_created and playback_state.display_window then
    pcall(vim.api.nvim_win_close, playback_state.display_window, false)
  end

  if opts.restore_focus then
    restore_window_state(playback_state.focus_state)
    if playback_state.focus_state and playback_state.focus_state.window_id and vim.api.nvim_win_is_valid(playback_state.focus_state.window_id) then
      pcall(vim.api.nvim_set_current_win, playback_state.focus_state.window_id)
    end
  end

  reset_playback_state()

  if opts.notify and had_active_walkthrough then
    vim.notify("AI walkthrough stopped", vim.log.levels.INFO)
  elseif opts.notify and not had_active_walkthrough then
    vim.notify("No AI walkthrough is currently running", vim.log.levels.WARN)
  end

  return had_active_walkthrough
end

local function finish_walkthrough()
  stop_timer()
  playback_state.waiting_for_voice = false
  playback_state.voice_started = false
  playback_state.pause_after_step = true
  playback_state.paused_for_continue = true
  playback_state.auto_pause_at_index = nil
  vim.notify("AI walkthrough complete", vim.log.levels.INFO)
end

local function ensure_display_window(target_path)
  if playback_state.display_window and vim.api.nvim_win_is_valid(playback_state.display_window) then
    local bufnr = vim.api.nvim_win_get_buf(playback_state.display_window)
    local buf_path = get_buffer_absolute_path(bufnr)

    if buf_path == target_path or not vim.bo[bufnr].modified then
      return playback_state.display_window
    end
  end

  local winid, created = select_display_window(target_path)
  if not winid then
    return nil
  end

  playback_state.display_window = winid
  playback_state.display_window_created = created
  playback_state.display_state = created and nil or capture_window_state(winid)

  return winid
end

local function show_walkthrough_step(step)
  local winid = ensure_display_window(step.resolved_path)
  if not winid then
    clear_active_step_highlight()
    return false, "AI walkthrough could not find a window to use"
  end

  local ok, err = pcall(function()
    vim.api.nvim_set_current_win(winid)

    local current_path = get_buffer_absolute_path(vim.api.nvim_win_get_buf(winid))
    if current_path ~= step.resolved_path then
      vim.cmd("edit " .. vim.fn.fnameescape(step.resolved_path))
    end

    local bufnr = vim.api.nvim_win_get_buf(winid)
    highlight_walkthrough_step(bufnr, step.line_start, step.line_end)
    focus_walkthrough_range(winid, step.line_start, step.line_end)
    show_walkthrough_description_box(winid, playback_state.index, step.line_start, step.description)
  end)

  if not ok then
    clear_active_step_highlight()
    close_walkthrough_description_box()
    return false, err
  end

  return true
end

local function schedule_next_step(delay_ms, callback)
  stop_timer()

  local timer = uv.new_timer()
  if not timer then
    vim.schedule(callback)
    return
  end

  playback_state.timer = timer

  timer:start(delay_ms, 0, function()
    timer:stop()
    timer:close()

    vim.schedule(function()
      if playback_state.timer == timer then
        playback_state.timer = nil
      end

      callback()
    end)
  end)
end

local should_pause_after_current_step

local function run_walkthrough_step(index)
  if not playback_state.active or not playback_state.steps then
    return
  end

  local step = playback_state.steps[index]
  if not step then
    finish_walkthrough()
    return
  end

  playback_state.index = index
  playback_state.paused_for_continue = false

  if playback_state.browser_window and vim.api.nvim_win_is_valid(playback_state.browser_window) then
    render_walkthrough_browser_window()
  end

  local shown, err = show_walkthrough_step(step)
  if not shown then
    vim.notify(err, vim.log.levels.WARN)
    run_walkthrough_step(index + 1)
    return
  end

  playback_state.waiting_for_voice = true
  playback_state.voice_started = false

  if not invoke_user_command("AIVoice", { step.description }) then
    playback_state.waiting_for_voice = false
    vim.notify("AI walkthrough could not start voice playback", vim.log.levels.WARN)

    if should_pause_after_current_step() then
      playback_state.paused_for_continue = true
    else
      schedule_next_step(get_post_voice_delay_ms(), function()
        run_walkthrough_step(index + 1)
      end)
    end
  end
end

should_pause_after_current_step = function()
  if playback_state.pause_after_step then
    return true
  end

  local target_index = tonumber(playback_state.auto_pause_at_index)
  if target_index and playback_state.index >= target_index then
    playback_state.auto_pause_at_index = nil
    playback_state.pause_after_step = true
    return true
  end

  return false
end

local function start_walkthrough(steps, opts)
  opts = opts or {}
  steps = prepare_walkthrough_steps(steps)

  stop_walkthrough({
    stop_voice = true,
    force_stop_voice = true,
    restore_display = false,
    restore_focus = false,
    close_created_window = false,
    notify = false,
  })

  playback_state.active = true
  playback_state.steps = vim.deepcopy(steps)
  playback_state.index = 0
  playback_state.pause_after_step = opts.pause_after_step == true
  playback_state.paused_for_continue = false
  playback_state.auto_pause_at_index = nil
  playback_state.focus_state = capture_window_state(vim.api.nvim_get_current_win())

  local first_step = playback_state.steps[1]
  local display_window, created = select_display_window(first_step.resolved_path)
  if not display_window then
    reset_playback_state()
    vim.notify("AI walkthrough could not create a display window", vim.log.levels.ERROR)
    return false
  end

  playback_state.display_window = display_window
  playback_state.display_window_created = created
  playback_state.display_state = created and nil or capture_window_state(display_window)
  last_walkthrough = vim.deepcopy(playback_state.steps)
  register_walkthrough_keymaps()

  run_walkthrough_step(1)
  return true
end

local function next_walkthrough_step()
  if not playback_state.active or not playback_state.steps then
    vim.notify("No AI walkthrough is currently running", vim.log.levels.WARN)
    return
  end

  if playback_state.waiting_for_voice then
    stop_timer()
    playback_state.waiting_for_voice = false
    playback_state.voice_started = false
    playback_state.paused_for_continue = false
    playback_state.auto_pause_at_index = nil
    invoke_user_command("AIVoiceStop")
    run_walkthrough_step(playback_state.index + 1)
    return
  end

  if not playback_state.pause_after_step then
    vim.notify("AI walkthrough is not paused for manual continuation", vim.log.levels.WARN)
    return
  end

  if not playback_state.paused_for_continue then
    vim.notify("AI walkthrough is not waiting for continue", vim.log.levels.WARN)
    return
  end

  playback_state.auto_pause_at_index = nil
  run_walkthrough_step(playback_state.index + 1)
end

local function continue_walkthrough(step_number)
  if not playback_state.active or not playback_state.steps then
    vim.notify("No AI walkthrough is currently running", vim.log.levels.WARN)
    return
  end

  local target_index = nil
  if step_number ~= nil then
    target_index = tonumber(step_number)
    if not target_index then
      vim.notify("AIWalkContinue step must be a number", vim.log.levels.WARN)
      return
    end

    target_index = math.floor(target_index)
    if target_index < 1 then
      vim.notify("AIWalkContinue step must be at least 1", vim.log.levels.WARN)
      return
    end
  end

  if target_index and not playback_state.waiting_for_voice and playback_state.index >= target_index then
    playback_state.pause_after_step = true
    playback_state.paused_for_continue = true
    playback_state.auto_pause_at_index = nil
    vim.notify("AI walkthrough is already at or past that step", vim.log.levels.INFO)
    return
  end

  local was_paused_for_continue = playback_state.paused_for_continue
  playback_state.pause_after_step = false
  playback_state.paused_for_continue = false
  playback_state.auto_pause_at_index = target_index

  if playback_state.waiting_for_voice then
    return
  end

  if playback_state.timer then
    return
  end

  if not was_paused_for_continue then
    return
  end

  stop_timer()
  run_walkthrough_step(playback_state.index + 1)
end

pause_walkthrough = function()
  if not playback_state.active or not playback_state.steps then
    vim.notify("No AI walkthrough is currently running", vim.log.levels.WARN)
    return
  end

  playback_state.pause_after_step = true
  playback_state.auto_pause_at_index = nil

  if playback_state.waiting_for_voice then
    vim.notify("AI walkthrough will pause after the current step", vim.log.levels.INFO)
    return
  end

  stop_timer()
  playback_state.paused_for_continue = true
  vim.notify("AI walkthrough paused", vim.log.levels.INFO)
end

local function toggle_walkthrough_playback()
  if not playback_state.active or not playback_state.steps then
    vim.notify("No AI walkthrough is currently running", vim.log.levels.WARN)
    return
  end

  if playback_state.pause_after_step and playback_state.paused_for_continue and not playback_state.waiting_for_voice then
    continue_walkthrough()
    return
  end

  pause_walkthrough()
end

local function replay_walkthrough_step(index)
  if not playback_state.active or not playback_state.steps then
    vim.notify("No AI walkthrough is currently running", vim.log.levels.WARN)
    return
  end

  local step = playback_state.steps[index]
  if not step then
    vim.notify("No previous AI walkthrough step is available", vim.log.levels.WARN)
    return
  end

  stop_timer()
  playback_state.paused_for_continue = false
  playback_state.auto_pause_at_index = nil
  playback_state.waiting_for_voice = false
  playback_state.voice_started = false
  invoke_user_command("AIVoiceStop")
  run_walkthrough_step(index)
end

local function previous_walkthrough_step()
  if not playback_state.active or not playback_state.steps then
    vim.notify("No AI walkthrough is currently running", vim.log.levels.WARN)
    return
  end

  local previous_index = playback_state.index - 1
  if previous_index < 1 then
    vim.notify("Already at the first AI walkthrough step", vim.log.levels.WARN)
    return
  end

  replay_walkthrough_step(previous_index)
end

local function jump_to_parent_walkthrough_step()
  if not playback_state.active or not playback_state.steps then
    vim.notify("No AI walkthrough is currently running", vim.log.levels.WARN)
    return
  end

  local current_step = playback_state.steps[playback_state.index]
  if not current_step then
    vim.notify("No current AI walkthrough step is available", vim.log.levels.WARN)
    return
  end

  if current_step.parent_id == nil then
    vim.notify("Current AI walkthrough step has no parent", vim.log.levels.INFO)
    return
  end

  local parent_index = find_step_index_by_id(playback_state.steps, current_step.parent_id)
  if not parent_index then
    vim.notify("Parent AI walkthrough step could not be found", vim.log.levels.WARN)
    return
  end

  replay_walkthrough_step(parent_index)
end

local function get_step_depth(steps, index)
  local depth = 0
  local seen = {}
  local current_index = index

  while current_index and current_index >= 1 and steps[current_index] do
    local parent_id = steps[current_index].parent_id
    if parent_id == nil then
      break
    end

    local parent_index = find_step_index_by_id(steps, parent_id)
    if not parent_index or seen[parent_index] then
      break
    end

    seen[parent_index] = true
    depth = depth + 1
    current_index = parent_index
  end

  return depth
end

local function remove_walkthrough_branch(step_id)
  local current_steps = playback_state.steps
  if type(current_steps) ~= "table" or #current_steps == 0 then
    return nil, nil, 0
  end

  local remove_index = find_step_index_by_id(current_steps, step_id)
  if not remove_index then
    return nil, nil, 0
  end

  local descendants = collect_descendant_step_ids(current_steps, step_id)
  local removed_count = 0
  local remaining = {}
  local current_step_id = current_steps[playback_state.index] and current_steps[playback_state.index].id or nil

  for _, step in ipairs(current_steps) do
    if descendants[step.id] ~= nil then
      removed_count = removed_count + 1
    else
      table.insert(remaining, vim.deepcopy(step))
    end
  end

  playback_state.steps = remaining
  last_walkthrough = vim.deepcopy(remaining)

  local next_index = nil
  if current_step_id then
    next_index = find_step_index_by_id(remaining, current_step_id)
  end

  if not next_index and #remaining > 0 then
    next_index = math.min(remove_index, #remaining)
  end

  return remaining, next_index, removed_count
end

find_step_index_by_id = function(steps, step_id)
  if type(steps) ~= "table" then
    return nil
  end

  for index, step in ipairs(steps) do
    if step.id == step_id then
      return index
    end
  end

  return nil
end

collect_descendant_step_ids = function(steps, ancestor_step_id)
  local descendants = {}
  local changed = true

  descendants[ancestor_step_id] = false

  while changed do
    changed = false
    for _, step in ipairs(steps) do
      if step.id ~= ancestor_step_id and step.parent_id ~= nil and descendants[step.parent_id] ~= nil and descendants[step.id] == nil then
        descendants[step.id] = true
        changed = true
      end
    end
  end

  return descendants
end

local function remove_walkthrough_descendants(anchor_step_id)
  local current_steps = playback_state.steps
  if type(current_steps) ~= "table" or #current_steps == 0 then
    return nil, 0
  end

  local anchor_index = find_step_index_by_id(current_steps, anchor_step_id)
  if not anchor_index then
    return nil, 0
  end

  local descendants = collect_descendant_step_ids(current_steps, anchor_step_id)
  local removed_count = 0
  local remaining = {}

  for _, step in ipairs(current_steps) do
    if descendants[step.id] == true then
      removed_count = removed_count + 1
    else
      table.insert(remaining, vim.deepcopy(step))
    end
  end

  playback_state.steps = remaining
  last_walkthrough = vim.deepcopy(remaining)

  local updated_anchor_index = find_step_index_by_id(remaining, anchor_step_id)
  return updated_anchor_index, removed_count
end

local function remove_current_walkthrough_enquiry()
  if not playback_state.active or not playback_state.steps then
    vim.notify("No AI walkthrough is currently running", vim.log.levels.WARN)
    return
  end

  local current_step = playback_state.steps[playback_state.index]
  if not current_step then
    vim.notify("No current AI walkthrough step is available", vim.log.levels.WARN)
    return
  end

  local anchor_step_id = current_step.parent_id or current_step.id
  local anchor_index, removed_count = remove_walkthrough_descendants(anchor_step_id)
  if not anchor_index then
    vim.notify("AI walkthrough could not remove the enquiry steps", vim.log.levels.WARN)
    return
  end

  if removed_count == 0 then
    vim.notify("No enquiry steps were attached to that walkthrough step", vim.log.levels.INFO)
  else
    vim.notify(string.format("Removed %d AI walkthrough enquiry step%s", removed_count, removed_count == 1 and "" or "s"), vim.log.levels.INFO)
  end

  replay_walkthrough_step(anchor_index)
end

local function shorten_walkthrough_path(path)
  path = tostring(path or "")
  if path == "" then
    return path
  end

  local separator = path:match("\\") and "\\" or "/"
  local parts = vim.split(path, separator, { plain = true, trimempty = false })

  if #parts <= 4 then
    return path
  end

  if separator == "\\" and parts[1]:match("^[A-Za-z]:$") then
    return table.concat({ parts[1], "...", parts[#parts - 2], parts[#parts - 1], parts[#parts] }, separator)
  end

  if parts[1] == "" then
    return table.concat({ "", "...", parts[#parts - 2], parts[#parts - 1], parts[#parts] }, separator)
  end

  return table.concat({ parts[1], "...", parts[#parts - 2], parts[#parts - 1], parts[#parts] }, separator)
end

local function remove_walkthrough_step_ids(step_ids)
  local current_steps = playback_state.steps
  if type(current_steps) ~= "table" or #current_steps == 0 then
    return nil, nil, 0
  end

  local removed_count = 0
  local remaining = {}
  local current_step_id = current_steps[playback_state.index] and current_steps[playback_state.index].id or nil
  local first_removed_index = nil

  for index, step in ipairs(current_steps) do
    if step_ids[step.id] then
      removed_count = removed_count + 1
      if not first_removed_index then
        first_removed_index = index
      end
    else
      table.insert(remaining, vim.deepcopy(step))
    end
  end

  playback_state.steps = remaining
  last_walkthrough = vim.deepcopy(remaining)

  local next_index = nil
  if current_step_id then
    next_index = find_step_index_by_id(remaining, current_step_id)
  end

  if not next_index and #remaining > 0 then
    next_index = math.min(first_removed_index or #remaining, #remaining)
  end

  return remaining, next_index, removed_count
end

local function build_walkthrough_browser_lines()
  local steps = playback_state.steps or {}
  local lines = {
    "AI Walkthrough",
    "<Enter>: jump  d/x: delete step tree  D: delete siblings  Q: end walkthrough  q: close",
    "",
  }
  local line_to_step_index = {}

  for index, step in ipairs(steps) do
    local depth = get_step_depth(steps, index)
    local marker = index == playback_state.index and ">" or " "
    local label = step.parent_id and "Child" or "Step"
    local summary = trim(step.description):gsub("%s+", " ")
    local display_path = shorten_walkthrough_path(step.path)
    if vim.fn.strdisplaywidth(summary) > 72 then
      summary = vim.fn.strcharpart(summary, 0, 69) .. "..."
    end

    local text = string.format("%s%s[%d] %s %s:%d - %s", marker, string.rep("  ", depth), index, label, display_path, step.line_start, summary)
    table.insert(lines, text)
    line_to_step_index[#lines] = index
  end

  return lines, line_to_step_index
end

render_walkthrough_browser_window = function()
  if not playback_state.browser_buffer or not vim.api.nvim_buf_is_valid(playback_state.browser_buffer) then
    return false
  end

  local lines, line_to_step_index = build_walkthrough_browser_lines()
  vim.bo[playback_state.browser_buffer].modifiable = true
  vim.api.nvim_buf_set_lines(playback_state.browser_buffer, 0, -1, false, lines)
  vim.bo[playback_state.browser_buffer].modifiable = false
  vim.b[playback_state.browser_buffer].ai_walkthrough_line_to_step_index = line_to_step_index

  if playback_state.browser_window and vim.api.nvim_win_is_valid(playback_state.browser_window) then
    local target_line = 4
    for line_number, step_index in pairs(line_to_step_index) do
      if step_index == playback_state.index then
        target_line = line_number
        break
      end
    end

    pcall(vim.api.nvim_win_set_cursor, playback_state.browser_window, { target_line, 0 })
  end

  return true
end

local function browser_window_step_index()
  if not playback_state.browser_buffer or not vim.api.nvim_buf_is_valid(playback_state.browser_buffer) then
    return nil
  end

  local cursor = vim.api.nvim_win_get_cursor(0)
  local mapping = vim.b[playback_state.browser_buffer].ai_walkthrough_line_to_step_index or {}
  return mapping[cursor[1]]
end

local function browser_window_jump_to_step()
  local step_index = browser_window_step_index()
  if not step_index then
    return
  end

  close_walkthrough_browser_window()
  replay_walkthrough_step(step_index)
end

local function browser_window_delete_step()
  local step_index = browser_window_step_index()
  local steps = playback_state.steps or {}
  local step = step_index and steps[step_index] or nil
  if not step then
    return
  end

  stop_timer()
  playback_state.waiting_for_voice = false
  playback_state.voice_started = false
  playback_state.paused_for_continue = false
  playback_state.auto_pause_at_index = nil
  invoke_user_command("AIVoiceStop")

  local remaining, next_index, removed_count = remove_walkthrough_branch(step.id)
  if removed_count == 0 then
    return
  end

  if not remaining or #remaining == 0 then
    stop_walkthrough({
      stop_voice = false,
      restore_display = true,
      restore_focus = true,
      close_created_window = true,
      notify = true,
    })
    vim.notify("Removed the final AI walkthrough step tree", vim.log.levels.INFO)
    return
  end

  render_walkthrough_browser_window()

  if next_index then
    replay_walkthrough_step(next_index)
    render_walkthrough_browser_window()
  end

  focus_walkthrough_browser_window()

  vim.notify(string.format("Removed %d AI walkthrough step%s", removed_count, removed_count == 1 and "" or "s"), vim.log.levels.INFO)
end

local function browser_window_delete_siblings()
  local step_index = browser_window_step_index()
  local steps = playback_state.steps or {}
  local step = step_index and steps[step_index] or nil
  if not step then
    return
  end

  stop_timer()
  playback_state.waiting_for_voice = false
  playback_state.voice_started = false
  playback_state.paused_for_continue = false
  playback_state.auto_pause_at_index = nil
  invoke_user_command("AIVoiceStop")

  local remove_ids = {}

  for _, candidate in ipairs(steps) do
    if candidate.parent_id == step.parent_id then
      local descendants = collect_descendant_step_ids(steps, candidate.id)
      for descendant_id in pairs(descendants) do
        remove_ids[descendant_id] = true
      end
    end
  end

  local remaining, next_index, removed_count = remove_walkthrough_step_ids(remove_ids)
  if removed_count == 0 then
    return
  end

  if not remaining or #remaining == 0 then
    stop_walkthrough({
      stop_voice = false,
      restore_display = true,
      restore_focus = true,
      close_created_window = true,
      notify = true,
    })
    vim.notify("Removed the final AI walkthrough sibling group", vim.log.levels.INFO)
    return
  end

  render_walkthrough_browser_window()

  if next_index then
    replay_walkthrough_step(next_index)
    render_walkthrough_browser_window()
  end

  focus_walkthrough_browser_window()

  vim.notify(string.format("Removed %d AI walkthrough sibling step%s", removed_count, removed_count == 1 and "" or "s"), vim.log.levels.INFO)
end

local function browser_window_stop_walkthrough()
  close_walkthrough_browser_window()

  vim.schedule(function()
    stop_walkthrough({
      stop_voice = true,
      restore_display = true,
      restore_focus = true,
      close_created_window = true,
      notify = true,
    })
  end)
end

local function open_walkthrough_browser_window()
  if not playback_state.active or not playback_state.steps or #playback_state.steps == 0 then
    vim.notify("No AI walkthrough is currently running", vim.log.levels.WARN)
    return
  end

  if playback_state.browser_window and vim.api.nvim_win_is_valid(playback_state.browser_window) then
    render_walkthrough_browser_window()
    pcall(vim.api.nvim_set_current_win, playback_state.browser_window)
    return
  end

  local bufnr = vim.api.nvim_create_buf(false, true)
  if not bufnr then
    return
  end

  local width = math.max(60, math.min(vim.o.columns - 2, math.floor(vim.o.columns * 0.95)))
  local height = math.min(math.max(10, #playback_state.steps + 4), math.max(8, vim.o.lines - 6))
  local row = math.max(1, math.floor((vim.o.lines - height) / 2) - 1)
  local col = math.max(1, math.floor((vim.o.columns - width) / 2))

  local ok, winid = pcall(vim.api.nvim_open_win, bufnr, true, {
    relative = "editor",
    row = row,
    col = col,
    width = width,
    height = height,
    style = "minimal",
    border = "rounded",
    title = " AI Walkthrough ",
    title_pos = "center",
    zindex = 200,
  })

  if not ok then
    pcall(vim.api.nvim_buf_delete, bufnr, { force = true })
    return
  end

  playback_state.browser_buffer = bufnr
  playback_state.browser_window = winid

  vim.bo[bufnr].bufhidden = "wipe"
  vim.bo[bufnr].buftype = "nofile"
  vim.bo[bufnr].swapfile = false
  vim.bo[bufnr].modifiable = false
  vim.wo[winid].cursorline = true
  vim.wo[winid].wrap = false

  local opts = { buffer = bufnr, silent = true, nowait = true }
  vim.keymap.set("n", "q", close_walkthrough_browser_window, opts)
  vim.keymap.set("n", "<Esc>", close_walkthrough_browser_window, opts)
  vim.keymap.set("n", "Q", browser_window_stop_walkthrough, opts)
  vim.keymap.set("n", "<CR>", browser_window_jump_to_step, opts)
  vim.keymap.set("n", "d", browser_window_delete_step, opts)
  vim.keymap.set("n", "x", browser_window_delete_step, opts)
  vim.keymap.set("n", "dd", browser_window_delete_step, opts)
  vim.keymap.set("n", "D", browser_window_delete_siblings, opts)

  render_walkthrough_browser_window()
end

local function insert_walkthrough_steps(parent_step_id, new_steps)
  if type(new_steps) ~= "table" or #new_steps == 0 then
    return false
  end

  local current_steps = playback_state.steps
  if type(current_steps) ~= "table" or #current_steps == 0 then
    return false
  end

  local parent_index = find_step_index_by_id(current_steps, parent_step_id)

  if not parent_index then
    return false
  end

  local descendants = collect_descendant_step_ids(current_steps, parent_step_id)

  local prepared_steps = prepare_walkthrough_steps(new_steps, parent_step_id)
  if #prepared_steps == 0 then
    return false
  end

  local combined = {}
  local inserted = false

  for _, step in ipairs(current_steps) do
    if step.id == parent_step_id then
      table.insert(combined, vim.deepcopy(step))
      for _, new_step in ipairs(prepared_steps) do
        table.insert(combined, vim.deepcopy(new_step))
      end
      inserted = true
    elseif descendants[step.id] == nil then
      table.insert(combined, vim.deepcopy(step))
    end
  end

  if not inserted then
    return false
  end

  playback_state.steps = combined
  last_walkthrough = vim.deepcopy(combined)
  if playback_state.browser_window and vim.api.nvim_win_is_valid(playback_state.browser_window) then
    render_walkthrough_browser_window()
  end
  return #prepared_steps
end

local function handle_walkthrough_response(chat)
  local response = get_latest_assistant_raw_response(chat)
  if not response or not has_walkthrough_identifier(response) then
    return false
  end

  local steps, err = parse_walkthrough_response(response)
  if not steps then
    pending_walkthrough_request = nil
    vim.notify("AI walkthrough response detected, but it could not be parsed: " .. err, vim.log.levels.WARN)
    return true
  end

  local request = pending_walkthrough_request or { mode = "start", start_opts = {} }
  pending_walkthrough_request = nil

  if request.mode == "enquire" then
    local inserted_count = insert_walkthrough_steps(request.parent_step_id, steps)
    if inserted_count then
      local pause_at_index = playback_state.index + inserted_count
      vim.notify(string.format("Inserted %d AI walkthrough answer step%s", inserted_count, inserted_count == 1 and "" or "s"), vim.log.levels.INFO)
      continue_walkthrough(pause_at_index)
    else
      vim.notify("AI walkthrough answer response was parsed, but there was no active walkthrough step to update", vim.log.levels.WARN)
    end

    return true
  end

  local start_opts = request.start_opts or {}

  vim.defer_fn(function()
    start_walkthrough(steps, start_opts)
  end, 20)

  return true
end

local function reset_walkthrough()
  if not playback_state.active or not playback_state.steps or #playback_state.steps == 0 then
    vim.notify("No AI walkthrough is currently running", vim.log.levels.WARN)
    return
  end

  start_walkthrough(playback_state.steps, {
    pause_after_step = playback_state.pause_after_step,
  })
end

local function export_walkthrough(file_arg)
  local steps = playback_state.steps or last_walkthrough
  if type(steps) ~= "table" or #steps == 0 then
    vim.notify("No AI walkthrough is available to export", vim.log.levels.WARN)
    return
  end

  local target = trim(file_arg or "")
  if target == "" then
    vim.notify("AIWalkExport requires a file path", vim.log.levels.WARN)
    return
  end

  local path = vim.fn.fnamemodify(vim.fn.expand(target), ":p")
  local lines = { "# ai-walkthrough" }

  for _, step in ipairs(steps) do
    table.insert(lines, string.format("- id: %d", tonumber(step.id) or 0))
    if step.parent_id ~= nil then
      table.insert(lines, string.format("  parent_id: %d", tonumber(step.parent_id) or 0))
    end
    table.insert(lines, "  path: " .. yaml_quote(step.path))
    table.insert(lines, string.format("  line_start: %d", step.line_start))
    table.insert(lines, string.format("  line_end: %d", step.line_end))
    table.insert(lines, "  description: " .. yaml_quote(step.description))
  end

  local ok, err = pcall(vim.fn.writefile, lines, path)
  if not ok then
    vim.notify("AI walkthrough export failed: " .. tostring(err), vim.log.levels.ERROR)
    return
  end

  vim.notify("AI walkthrough exported to " .. path, vim.log.levels.INFO)
end

local function import_walkthrough(file_arg)
  local target = trim(file_arg or "")
  if target == "" then
    vim.notify("AIWalkImport requires a file path", vim.log.levels.WARN)
    return
  end

  local path = vim.fn.fnamemodify(vim.fn.expand(target), ":p")
  if vim.fn.filereadable(path) ~= 1 then
    vim.notify("AI walkthrough import file not found: " .. path, vim.log.levels.WARN)
    return
  end

  local ok, lines = pcall(vim.fn.readfile, path)
  if not ok then
    vim.notify("AI walkthrough import failed: " .. tostring(lines), vim.log.levels.ERROR)
    return
  end

  local steps, err = parse_walkthrough_response(table.concat(lines, "\n"))
  if not steps then
    vim.notify("AI walkthrough import could not parse YAML: " .. tostring(err), vim.log.levels.WARN)
    return
  end

  start_walkthrough(steps)
  vim.notify("AI walkthrough imported from " .. path, vim.log.levels.INFO)
end

_G.ai_walkthrough_handle_codecompanion_response = handle_walkthrough_response
_G.ai_walkthrough_stop_current = stop_walkthrough

set_walkthrough_highlight()

create_or_replace_user_command("AIWalk", function(opts)
  send_walkthrough_request(opts)
end, {
  nargs = "*",
  range = true,
  desc = "Append an AI walkthrough prompt to the active CodeCompanion chat",
})

create_or_replace_user_command("AIWalkSlow", function(opts)
  send_walkthrough_request(opts, {
    pause_after_step = true,
  })
end, {
  nargs = "*",
  range = true,
  desc = "Append an AI walkthrough prompt and start playback in slow mode",
})

create_or_replace_user_command("AIWalkStop", function()
  stop_walkthrough({
    stop_voice = true,
    restore_display = true,
    restore_focus = true,
    close_created_window = true,
    notify = true,
  })
end, {
  desc = "Stop the active AI walkthrough",
})

create_or_replace_user_command("AIWalkRepeat", function()
  if not last_walkthrough or #last_walkthrough == 0 then
    vim.notify("No AI walkthrough is available to repeat", vim.log.levels.WARN)
    return
  end

  start_walkthrough(last_walkthrough)
end, {
  desc = "Repeat the last AI walkthrough",
})

create_or_replace_user_command("AIWalkRepeatSlow", function()
  if not last_walkthrough or #last_walkthrough == 0 then
    vim.notify("No AI walkthrough is available to repeat", vim.log.levels.WARN)
    return
  end

  start_walkthrough(last_walkthrough, {
    pause_after_step = true,
  })
end, {
  desc = "Repeat the last AI walkthrough, pausing after each step",
})

create_or_replace_user_command("AIWalkNext", function()
  next_walkthrough_step()
end, {
  desc = "Advance a paused AI walkthrough by one step",
})

create_or_replace_user_command("AIWalkContinue", function(opts)
  local step_number = trim(opts.args or "")
  if step_number == "" then
    step_number = nil
  end

  continue_walkthrough(step_number)
end, {
  nargs = "?",
  desc = "Resume automatic AI walkthrough playback, optionally pausing again at a step",
})

create_or_replace_user_command("AIWalkToggle", function()
  toggle_walkthrough_playback()
end, {
  desc = "Toggle AI walkthrough playback",
})

create_or_replace_user_command("AIWalkPause", function()
  pause_walkthrough()
end, {
  desc = "Pause the active AI walkthrough after the current step",
})

create_or_replace_user_command("AIWalkPrev", function()
  previous_walkthrough_step()
end, {
  desc = "Replay the previous AI walkthrough step",
})

create_or_replace_user_command("AIWalkParent", function()
  jump_to_parent_walkthrough_step()
end, {
  desc = "Jump to the parent AI walkthrough step",
})

create_or_replace_user_command("AIWalkPrevious", function()
  previous_walkthrough_step()
end, {
  desc = "Replay the previous AI walkthrough step",
})

create_or_replace_user_command("AIWalkThread", function(opts)
  request_walkthrough_enquiry(opts)
end, {
  nargs = "*",
  desc = "Create an AI follow-up thread from the current walkthrough step",
})

create_or_replace_user_command("AIWalkInstruct", function(opts)
  request_walkthrough_instruction(opts)
end, {
  nargs = "*",
  desc = "Send an instruction to AI using the current walkthrough step as context",
})

create_or_replace_user_command("AIWalkRemoveEnquiry", function()
  remove_current_walkthrough_enquiry()
end, {
  desc = "Remove enquiry steps for the current walkthrough branch",
})

create_or_replace_user_command("AIWalkRestart", function()
  reset_walkthrough()
end, {
  desc = "Restart the active AI walkthrough from the first step",
})

create_or_replace_user_command("AIWalkWindow", function()
  open_walkthrough_browser_window()
end, {
  desc = "Open a floating window for browsing AI walkthrough steps",
})

create_or_replace_user_command("AIWalkExport", function(opts)
  export_walkthrough(opts.args)
end, {
  nargs = 1,
  complete = "file",
  desc = "Export the current AI walkthrough to a YAML file",
})

create_or_replace_user_command("AIWalkImport", function(opts)
  import_walkthrough(opts.args)
end, {
  nargs = 1,
  complete = "file",
  desc = "Import an AI walkthrough from a YAML file",
})

vim.api.nvim_create_autocmd("User", {
  group = walkthrough_augroup,
  pattern = "AIVoicePlaybackStarted",
  callback = function()
    if not playback_state.active or not playback_state.waiting_for_voice then
      return
    end

    playback_state.voice_started = true
  end,
})

vim.api.nvim_create_autocmd("User", {
  group = walkthrough_augroup,
  pattern = "AIVoicePlaybackStopped",
  callback = function()
    if not playback_state.active or not playback_state.waiting_for_voice or not playback_state.voice_started then
      return
    end

    local next_index = playback_state.index + 1
    playback_state.waiting_for_voice = false
    playback_state.voice_started = false

    if should_pause_after_current_step() then
      playback_state.paused_for_continue = true
    else
      schedule_next_step(get_post_voice_delay_ms(), function()
        run_walkthrough_step(next_index)
      end)
    end
  end,
})

vim.api.nvim_create_autocmd("ColorScheme", {
  group = walkthrough_augroup,
  callback = set_walkthrough_highlight,
})
