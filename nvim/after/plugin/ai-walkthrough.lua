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

local playback_state = {
  active = false,
  steps = nil,
  index = 0,
  timer = nil,
  waiting_for_voice = false,
  voice_started = false,
  pause_after_step = false,
  paused_for_continue = false,
  focus_state = nil,
  display_state = nil,
  display_window = nil,
  display_window_created = false,
  highlighted_buffer = nil,
  description_buffer = nil,
  description_window = nil,
}

local last_walkthrough = nil
local pending_walkthrough_start_opts = nil

local function create_or_replace_user_command(name, fn, opts)
  pcall(vim.api.nvim_del_user_command, name)
  vim.api.nvim_create_user_command(name, fn, opts)
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
    "You are creating an editor walkthrough for the user's query.",
    "Reply with valid YAML only.",
    "The response must include the exact identifier comment `# ai-walkthrough` immediately before the YAML array.",
    "Return a top-level YAML array of steps.",
    "Each step must include `path`, `line_start`, `line_end`, and `description`.",
    "`path` must point to a real file and should usually be repo-relative.",
    "`line_start` and `line_end` must be 1-based line numbers that define the inclusive range to highlight for that step.",
    "Keep ranges focused on the most relevant block, and avoid ranges that span an entire file unless the file is truly tiny.",
    "If a step only needs one line, set `line_start` and `line_end` to the same value.",
    "`description` must be concise natural narration that sounds good when spoken aloud.",
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

local function send_walkthrough_request(opts, start_opts)
  local chat, err = get_target_chat()
  if not chat then
    vim.notify(err, vim.log.levels.WARN)
    return
  end

  pending_walkthrough_start_opts = vim.deepcopy(start_opts or {})
  send_message_to_chat(chat, build_walkthrough_message(opts))
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
      local description = trim(get_table_value(step, { "description", "walkthrough_description", "walkthrough", "text" }))
      local resolved_path = resolve_step_path(path)

      if path ~= "" and line_start and line_start >= 1 and line_end and line_end >= 1 and description ~= "" and resolved_path then
        local normalized_line_start = math.max(1, math.floor(math.min(line_start, line_end)))
        local normalized_line_end = math.max(normalized_line_start, math.floor(math.max(line_start, line_end)))

        table.insert(steps, {
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

local function show_walkthrough_description_box(winid, line_start, description)
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

  local wrapped = wrap_text(description, inner_width)
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
  playback_state.active = false
  playback_state.steps = nil
  playback_state.index = 0
  playback_state.waiting_for_voice = false
  playback_state.voice_started = false
  playback_state.pause_after_step = false
  playback_state.paused_for_continue = false
  playback_state.focus_state = nil
  playback_state.display_state = nil
  playback_state.display_window = nil
  playback_state.display_window_created = false
  playback_state.highlighted_buffer = nil
  playback_state.description_buffer = nil
  playback_state.description_window = nil
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
  reset_playback_state()
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
    show_walkthrough_description_box(winid, step.line_start, step.description)
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

    if playback_state.pause_after_step then
      playback_state.paused_for_continue = true
    else
      schedule_next_step(get_post_voice_delay_ms(), function()
        run_walkthrough_step(index + 1)
      end)
    end
  end
end

local function start_walkthrough(steps, opts)
  opts = opts or {}

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

  run_walkthrough_step(1)
  return true
end

local function continue_walkthrough()
  if not playback_state.active or not playback_state.steps then
    vim.notify("No AI walkthrough is currently running", vim.log.levels.WARN)
    return
  end

  if playback_state.waiting_for_voice then
    vim.notify("AI walkthrough is still narrating the current step", vim.log.levels.WARN)
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

  run_walkthrough_step(playback_state.index + 1)
end

local function pause_walkthrough()
  if not playback_state.active or not playback_state.steps then
    vim.notify("No AI walkthrough is currently running", vim.log.levels.WARN)
    return
  end

  playback_state.pause_after_step = true

  if playback_state.waiting_for_voice then
    vim.notify("AI walkthrough will pause after the current step", vim.log.levels.INFO)
    return
  end

  stop_timer()
  playback_state.paused_for_continue = true
  vim.notify("AI walkthrough paused", vim.log.levels.INFO)
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

local function handle_walkthrough_response(chat)
  local response = get_latest_assistant_raw_response(chat)
  if not response or not has_walkthrough_identifier(response) then
    return false
  end

  local steps, err = parse_walkthrough_response(response)
  if not steps then
    pending_walkthrough_start_opts = nil
    vim.notify("AI walkthrough response detected, but it could not be parsed: " .. err, vim.log.levels.WARN)
    return true
  end

  local start_opts = pending_walkthrough_start_opts or {}
  pending_walkthrough_start_opts = nil

  vim.defer_fn(function()
    start_walkthrough(steps, start_opts)
  end, 20)

  return true
end

_G.ai_walkthrough_handle_codecompanion_response = handle_walkthrough_response
_G.ai_walkthrough_stop_current = stop_walkthrough

set_walkthrough_highlight()

create_or_replace_user_command("AIWalkthrough", function(opts)
  send_walkthrough_request(opts)
end, {
  nargs = "*",
  range = true,
  desc = "Append an AI walkthrough prompt to the active CodeCompanion chat",
})

create_or_replace_user_command("AIWalkthroughSlow", function(opts)
  send_walkthrough_request(opts, {
    pause_after_step = true,
  })
end, {
  nargs = "*",
  range = true,
  desc = "Append an AI walkthrough prompt and start playback in slow mode",
})

create_or_replace_user_command("AIWalkthroughStop", function()
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

create_or_replace_user_command("AIWalkthroughRepeat", function()
  if not last_walkthrough or #last_walkthrough == 0 then
    vim.notify("No AI walkthrough is available to repeat", vim.log.levels.WARN)
    return
  end

  start_walkthrough(last_walkthrough)
end, {
  desc = "Repeat the last AI walkthrough",
})

create_or_replace_user_command("AIWalkthroughRepeatSlow", function()
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

create_or_replace_user_command("AIWalkthroughContinue", function()
  continue_walkthrough()
end, {
  desc = "Continue a paused AI walkthrough",
})

create_or_replace_user_command("AIWalkthroughPause", function()
  pause_walkthrough()
end, {
  desc = "Pause the active AI walkthrough after the current step",
})

create_or_replace_user_command("AIWalkthroughPrevious", function()
  previous_walkthrough_step()
end, {
  desc = "Replay the previous AI walkthrough step",
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

    if playback_state.pause_after_step then
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
