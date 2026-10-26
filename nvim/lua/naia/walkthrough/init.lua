local function setup()
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
  { lhs = "[w", rhs = "<Cmd>AIWalkPrev<CR>", desc = "Replay previous AI walkthrough step" },
  { lhs = "]w", rhs = "<Cmd>AIWalkNext<CR>", desc = "Advance AI walkthrough by one step" },
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
local next_walkthrough_step_id = 1
local naia_walkthrough_tool_names = {
  context = "nvim_walkthrough_current_context",
  start = "nvim_walkthrough_start",
  insert = "nvim_walkthrough_insert_steps",
}
local find_step_index_by_id
local collect_descendant_step_ids
local pause_walkthrough
local render_walkthrough_browser_window
local rich = require("naia.walkthrough.tools")
local confirmation = require("naia.walkthrough.prompt")
local enhanced

function _G.ai_walkthrough_foldtext()
  local labels = vim.b.ai_walkthrough_fold_labels
  if type(labels) == "table" then
    local label = labels[vim.v.foldstart]
    if type(label) == "string" and label ~= "" then
      return label
    end
  end

  return vim.fn.getline(vim.v.foldstart)
end

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

  return 500
end

local function get_voice_disabled_extra_delay_ms()
  local delay_ms = tonumber(vim.g.ai_walkthrough_voice_disabled_extra_delay_ms)
  if delay_ms ~= nil then
    return math.max(0, math.floor(delay_ms))
  end

  local delay_seconds = tonumber(vim.g.ai_walkthrough_voice_disabled_extra_delay_seconds)
  if delay_seconds ~= nil then
    return math.max(0, math.floor(delay_seconds * 1000))
  end

  return 3000
end

local function get_inter_step_delay_ms(opts)
  opts = opts or {}

  local delay_ms = get_post_voice_delay_ms()
  if opts.voice_disabled then
    delay_ms = delay_ms + get_voice_disabled_extra_delay_ms()
  end

  return delay_ms
end

local function is_ai_voice_enabled()
  if type(_G.ai_voice_is_enabled) == "function" then
    return _G.ai_voice_is_enabled()
  end

  return vim.g.ai_voice_enabled ~= false
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
  path = uv.fs_realpath(path) or path

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

local function send_message_to_chat(chat, msg, fold_opts)
  fold_opts = fold_opts or {}

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
  local fold_start_line
  local fold_end_line

  if was_locked and chat.ui and chat.ui.unlock_buf then
    chat.ui:unlock_buf()
  end

  local line_count = vim.api.nvim_buf_line_count(chat.bufnr)
  local last_line = vim.api.nvim_buf_get_lines(chat.bufnr, line_count - 1, line_count, false)[1] or ""

  local ok, err = pcall(function()
    if last_line == "" then
      fold_start_line = line_count
      fold_end_line = line_count + #message_lines - 1
      vim.api.nvim_buf_set_lines(chat.bufnr, line_count - 1, line_count, false, message_lines)
    else
      fold_start_line = line_count + 2
      fold_end_line = line_count + #message_lines + 1
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

  if fold_start_line and fold_end_line then
    local exclude_trailing_lines = math.max(0, tonumber(fold_opts.exclude_trailing_lines) or 0)
    fold_end_line = math.max(fold_start_line, fold_end_line - exclude_trailing_lines)
  end

  local fold_labels = vim.b[chat.bufnr].ai_walkthrough_fold_labels
  if type(fold_labels) ~= "table" then
    fold_labels = {}
  end

  fold_labels[fold_start_line] = "User prompt for walkthrough:"
  vim.b[chat.bufnr].ai_walkthrough_fold_labels = fold_labels

  if chat.ui and chat.ui.winnr and vim.api.nvim_win_is_valid(chat.ui.winnr) and fold_start_line and fold_end_line then
    pcall(vim.api.nvim_win_call, chat.ui.winnr, function()
      vim.opt_local.foldmethod = "manual"
      vim.opt_local.foldtext = "v:lua.ai_walkthrough_foldtext()"
      vim.cmd(string.format("%d,%dfold", fold_start_line, fold_end_line))
      vim.cmd(string.format("%dfoldclose", fold_start_line))
    end)
  end

  if chat.ui:is_visible() then
    chat.ui:follow()
  end

  return true
end

local function build_enquiry_message(step, query)
  local lines = {
    "You are executing a user's follow-up prompt in relation to a single step from an existing text editor walkthrough.",
    "Use the `" .. naia_walkthrough_tool_names.insert .. "` MCP tool to insert the new steps.",
    "Do not reply with YAML or markdown code fences for the walkthrough.",
    "Return only the new in-between steps that should be inserted immediately after the current step.",
    "Each inserted step must include `path`, `line_start`, `line_end`, and `description`.",
    "Use repo-relative paths when possible, and keep descriptions natural for spoken narration.",
    "If the user has asked for you to make file changes, then you can do that before calling the tool.",
    "",
    "Current step:",
    string.format("id: %d", step.id),
    "walkthrough_id: " .. tostring(playback_state.walkthrough_id),
    "expected_revision: " .. tostring(playback_state.revision),
    "Append children without replacing earlier branches. Playback is a separate explicit action.",
    string.format("path: %s", step.path),
    string.format("line_start: %d", step.line_start),
    string.format("line_end: %d", step.line_end),
    string.format("description: %s", step.description),
    "",
    "User prompt:",
    query,
  }

  return table.concat(lines, "\n")
end

local function build_instruction_message(step, instruction)
  local lines = {
    "You are helping with a user's instruction about a single step from an existing text editor walkthrough.",
    "The user's name is Ollie",
    "Do not reply with YAML unless the user explicitly asks for it.",
    "If the best response is to extend the walkthrough itself, use the `" .. naia_walkthrough_tool_names.insert .. "` MCP tool.",
    "Use the current walkthrough step as the main context for your response.",
    "Stay tightly scoped to the same part of the codebase unless the instruction clearly requires adjacent context.",
    "",
    "Current step:",
    string.format("id: %d", step.id),
    "walkthrough_id: " .. tostring(playback_state.walkthrough_id),
    "expected_revision: " .. tostring(playback_state.revision),
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

  send_message_to_chat(chat, build_enquiry_message(current_step, query), {
    exclude_trailing_lines = query ~= "" and 1 or 0,
  })
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

enhanced = rich.new(playback_state, resolve_step_path)

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
          anchor = vim.deepcopy(step.anchor),
          beats = vim.deepcopy(step.beats),
          highlights = vim.deepcopy(step.highlights),
          comparison = vim.deepcopy(step.comparison),
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
  return require("naia.walkthrough.response").parse(text, normalize_walkthrough_steps)
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

  local ok = pcall(require("naia.walkthrough.layout").without_equalizing, function()
    vim.cmd(split_cmd)
  end)
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
    winbar = vim.wo[winid].winbar,
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

  if state.winbar ~= nil then
    pcall(vim.api.nvim_set_option_value, "winbar", state.winbar, { win = state.window_id })
  end

  if state.view then
    pcall(vim.api.nvim_win_call, state.window_id, function()
      vim.fn.winrestview(state.view)
    end)
  end

  return true
end

local function stop_timer()
  playback_state.timer_generation = (playback_state.timer_generation or 0) + 1
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

local function show_walkthrough_description_box(winid, step_index, line_start, description, step)
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
  local start_row = get_window_relative_line_row(winid, line_start) or 0
  local outer_height = box_height + 2
  local row = math.max(0, math.min(start_row - outer_height, math.max(0, height - outer_height)))
  local col = math.max(0, width - inner_width - 2)
  local relative = "win"
  if step then
    local panes = enhanced.comparison_panes or { { win = winid, loc = step } }
    local placement = require("naia.walkthrough.layout").place(winid, panes,
      step.highlights, box_height, row)
    if placement then
      row, box_height = placement.row, placement.height
      col = col + placement.col_origin
      relative = "editor"
    end
  end
  local display_lines = vim.list_slice(wrapped, 1, box_height)
  if box_height < #wrapped then
    local tail = display_lines[box_height] or ""
    display_lines[box_height] = vim.fn.strcharpart(tail, 0, math.max(0, inner_width - 3)) .. "..."
  end

  local bufnr = vim.api.nvim_create_buf(false, true)
  if not bufnr then
    return false
  end

  vim.bo[bufnr].bufhidden = "wipe"
  vim.bo[bufnr].modifiable = true
  vim.api.nvim_buf_set_lines(bufnr, 0, -1, false, display_lines)
  vim.bo[bufnr].modifiable = false

  local ok_float, float_win = pcall(vim.api.nvim_open_win, bufnr, false, {
    relative = relative,
    win = relative == "win" and winid or nil,
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
  local win_height = math.max(1, vim.fn.getwininfo(winid)[1].height)
  local range_height = normalized_end - normalized_start + 1
  local cursor_line = range_height <= win_height
      and math.floor((normalized_start + normalized_end) / 2)
    or normalized_start

  vim.api.nvim_win_set_cursor(winid, { cursor_line, 0 })
  vim.api.nvim_win_call(winid, function()
    -- Native scrolling accounts for wrapped lines, folds, diff filler, and
    -- the winbar. Unlike clamping topline, zz can center near EOF as well.
    vim.cmd("normal! zv")
    vim.cmd(range_height > win_height and "normal! zt" or "normal! zz")
  end)

  return true
end

local function reset_playback_state()
  enhanced:clear()
  clear_active_step_highlight()
  close_walkthrough_description_box()
  close_walkthrough_browser_window()
  unregister_walkthrough_keymaps()
  playback_state.active = false
  playback_state.steps = nil
  playback_state.index = 0
  playback_state.waiting_for_voice = false
  playback_state.voice_started = false
  playback_state.voice_request_id = nil
  playback_state.after_narration = nil
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
  confirmation.close()

  local had_active_walkthrough = playback_state.active or playback_state.timer ~= nil

  stop_timer()

  if opts.stop_voice and (had_active_walkthrough or opts.force_stop_voice) then
    invoke_user_command("AIVoiceStop")
  end

  -- Release the borrowed comparison window before restoring the user's layout.
  enhanced:cleanup_views()

  if opts.restore_display then
    restore_window_state(playback_state.display_state)
  end

  if opts.close_created_window and playback_state.display_window_created and playback_state.display_window then
    pcall(require("naia.walkthrough.layout").without_equalizing, function()
      vim.api.nvim_win_close(playback_state.display_window, false)
    end)
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
  playback_state.finished = true
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
  enhanced:cleanup_views()
  close_walkthrough_description_box()
  local resolved, effective = pcall(enhanced.effective, enhanced, step)
  if not resolved then return false, effective end
  step = effective
  local target = step.comparison and step.comparison.right or step
  local winid = step.comparison and playback_state.display_window or nil
  if not (winid and vim.api.nvim_win_is_valid(winid)) then
    winid = ensure_display_window(target.resolved_path)
  end
  if not winid then
    clear_active_step_highlight()
    return false, "AI walkthrough could not find a window to use"
  end

  local ok, err = pcall(function()
    vim.api.nvim_set_current_win(winid)
    enhanced:save_file_header(winid)

    if not step.comparison then
      local current_path = get_buffer_absolute_path(vim.api.nvim_win_get_buf(winid))
      if step.revision or step.text then
        vim.api.nvim_win_set_buf(winid, enhanced:buffer(step))
      elseif current_path ~= step.resolved_path then
        vim.cmd("edit " .. vim.fn.fnameescape(step.resolved_path))
      end
    end

    local focus = enhanced:render(step, winid)
    local bufnr = vim.api.nvim_win_get_buf(winid)
    highlight_walkthrough_step(bufnr, focus.line_start, focus.line_end)
    if step.comparison and step.comparison.diff then
      vim.api.nvim_win_call(winid, function() vim.cmd("diffupdate") end)
    end
    focus_walkthrough_range(winid, focus.line_start, focus.line_end)
    if step.comparison and step.comparison.diff then
      vim.api.nvim_win_call(winid, function()
        -- Synchronize the before pane after native diff-aware centering.
        vim.cmd("syncbind")
      end)
    end
    show_walkthrough_description_box(winid, playback_state.index, focus.line_start, step.description, step)
  end)

  if not ok then
    enhanced:cleanup_views()
    clear_active_step_highlight()
    close_walkthrough_description_box()
    return false, err
  end

  return true
end

local function schedule_next_step(delay_ms, callback)
  stop_timer()
  local generation = playback_state.timer_generation

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
      if generation ~= playback_state.timer_generation then return end
      if playback_state.timer == timer then
        playback_state.timer = nil
      end

      callback()
    end)
  end)
end

local should_pause_after_current_step
local advance_walkthrough_step

local function run_walkthrough_step(index, continue_beat)
  if not playback_state.active or not playback_state.steps then
    return
  end

  local step = playback_state.steps[index]
  if not step then
    finish_walkthrough()
    return
  end

  playback_state.index = index
  playback_state.playback_revision = (playback_state.playback_revision or 0) + 1
  playback_state.finished = false
  if not continue_beat then playback_state.beat_index = 1 end
  playback_state.paused_for_continue = false

  if playback_state.browser_window and vim.api.nvim_win_is_valid(playback_state.browser_window) then
    render_walkthrough_browser_window()
  end

  local shown, err = show_walkthrough_step(step)
  if not shown then
    vim.notify(err, vim.log.levels.WARN)
    playback_state.pause_after_step = true
    playback_state.paused_for_continue = true
    return
  end

  local function after_narration()
    -- A mutation may have inserted steps before this one while narration played.
    if not playback_state.active or not playback_state.steps[playback_state.index]
      or playback_state.steps[playback_state.index].id ~= step.id then return end
    if enhanced:next_beat() then
      schedule_next_step(get_inter_step_delay_ms({ voice_disabled = not is_ai_voice_enabled() }), function()
        run_walkthrough_step(playback_state.index, true)
      end)
    elseif should_pause_after_current_step() then
      playback_state.paused_for_continue = true
      playback_state.finished = playback_state.index == #playback_state.steps
    else
      schedule_next_step(get_inter_step_delay_ms({ voice_disabled = not is_ai_voice_enabled() }), function()
        run_walkthrough_step(playback_state.index + 1)
      end)
    end
  end
  playback_state.after_narration = after_narration

  if not is_ai_voice_enabled() then
    after_narration()
    return
  end

  playback_state.waiting_for_voice = true
  playback_state.voice_started = false
  playback_state.voice_request_id = nil

  if not invoke_user_command("AIVoiceInterrupt", { enhanced:effective(step).description }) then
    playback_state.waiting_for_voice = false
    vim.notify("AI walkthrough could not start voice playback", vim.log.levels.WARN)

    after_narration()
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
  -- Validate the whole tour before disturbing the currently playing one.
  steps = enhanced:normalize(steps)
  steps = prepare_walkthrough_steps(steps)

  if opts.confirm_start then
    confirmation.open(function(accepted)
      if not accepted then return end
      local ok, err = pcall(start_walkthrough, steps, vim.tbl_extend("force", opts, { confirm_start = false }))
      if not ok then vim.notify("AI walkthrough could not start: " .. tostring(err), vim.log.levels.ERROR) end
    end)
    return true, "awaiting_confirmation"
  end

  stop_walkthrough({
    stop_voice = true,
    force_stop_voice = true,
    restore_display = false,
    restore_focus = false,
    close_created_window = false,
    notify = false,
  })

  -- stop clears the previous tour's anchors; establish tracking for every new
  -- location now, including steps that have not been displayed yet.
  steps = enhanced:normalize(steps)
  playback_state.active = true
  enhanced:started()
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
    advance_walkthrough_step(playback_state.index + 1)
    return
  end

  if playback_state.timer then
    advance_walkthrough_step(playback_state.index + 1)
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
  advance_walkthrough_step(playback_state.index + 1)
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

advance_walkthrough_step = function(index)
  if not playback_state.active or not playback_state.steps then
    vim.notify("No AI walkthrough is currently running", vim.log.levels.WARN)
    return
  end

  local step = playback_state.steps[index]
  if not step then
    if index > (playback_state.index or 0) then
      finish_walkthrough()
    else
      vim.notify("No AI walkthrough step is available", vim.log.levels.WARN)
    end
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
  enhanced:touch()
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
  enhanced:touch()
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
  enhanced:touch()
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
    "<Enter>: jump  d/x: delete step tree  D: delete sibling group  Q: end walkthrough  q: close",
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

local function reset_walkthrough()
  if not playback_state.active or not playback_state.steps or #playback_state.steps == 0 then
    vim.notify("No AI walkthrough is currently running", vim.log.levels.WARN)
    return
  end

  start_walkthrough(playback_state.steps, {
    pause_after_step = playback_state.pause_after_step,
  })
end

local function export_walkthrough_file(args)
  local files = require("naia.walkthrough.file")
  local steps = playback_state.steps or last_walkthrough
  if type(steps) ~= "table" or #steps == 0 then error("No AI walkthrough is available to export", 0) end
  if type(args.path) ~= "string" or trim(args.path) == "" then error("Export requires a file path", 0) end
  local path = vim.fn.fnamemodify(vim.fn.expand(args.path), ":p")
  if (vim.uv or vim.loop).fs_lstat(path) and not args.overwrite then
    error("Walkthrough file already exists; use overwrite=true or AIWalkExport!", 0)
  end
  local text = enhanced:export(steps, files.root(args.root))
  if vim.fn.writefile({ text }, path) ~= 0 then error("Could not write walkthrough file", 0) end
  return { ok = true, version = 3, total_steps = #steps }
end

local function load_walkthrough_file(args)
  if type(args.path) ~= "string" or trim(args.path) == "" then error("Load requires a file path", 0) end
  local path = vim.fn.fnamemodify(vim.fn.expand(args.path), ":p")
  local text = table.concat(vim.fn.readfile(path), "\n")
  -- Recognize the old marker header too, so a versioned JSON file cannot
  -- accidentally bypass portable validation through the legacy parser.
  local data = require("naia.walkthrough.response").json(text)
  local steps
  if type(data) == "table" and data.version ~= nil then
    if data.version == 3 then
      steps = require("naia.walkthrough.file").decode(vim.json.encode(data), args.root)
    elseif data.version == 2 then
      steps = data.steps
    else error("Unsupported walkthrough version", 0) end
  else
    steps = parse_walkthrough_response(text)
  end
  if not steps then error("Could not parse walkthrough file", 0) end
  local started, reason = start_walkthrough(steps, {
    confirm_start = args.confirm_start ~= false, pause_after_step = args.pause_after_step == true,
  })
  if reason == "awaiting_confirmation" then
    return { status = "awaiting_confirmation", total_steps = #steps }
  end
  if not started then
    if reason == "cancelled" then return { cancelled = true } end
    error("Could not start walkthrough", 0)
  end
  local ids = {}
  for _, entry in ipairs(playback_state.steps) do ids[#ids + 1] = entry.id end
  return { walkthrough_id = playback_state.walkthrough_id, revision = playback_state.revision, step_ids = ids }
end

local function export_walkthrough(file_arg, overwrite)
  local ok, result = pcall(export_walkthrough_file, { path = file_arg, overwrite = overwrite })
  vim.notify(ok and "AI walkthrough exported" or tostring(result), ok and vim.log.levels.INFO or vim.log.levels.ERROR)
end

local function import_walkthrough(file_arg)
  local ok, result = pcall(load_walkthrough_file, { path = file_arg })
  if not ok then vim.notify(tostring(result), vim.log.levels.ERROR) end
end

_G.ai_walkthrough_handle_codecompanion_response = nil
_G.ai_walkthrough_stop_current = stop_walkthrough

local function register_naia_walkthrough_tools()
  local ok, naia = pcall(require, "naia")
  if not ok then return end
  rich.register(enhanced, naia, {
    export = export_walkthrough_file,
    load = load_walkthrough_file,
    start = start_walkthrough,
    prepare = prepare_walkthrough_steps,
    changed = function(steps)
      last_walkthrough = vim.deepcopy(steps)
      if playback_state.browser_window and vim.api.nvim_win_is_valid(playback_state.browser_window) then
        render_walkthrough_browser_window()
      end
    end,
    jump = replay_walkthrough_step,
    pause = pause_walkthrough,
    resume = continue_walkthrough,
    stop = function()
      stop_walkthrough({ stop_voice = true, restore_display = true, restore_focus = true, close_created_window = true })
    end,
  })
end

set_walkthrough_highlight()
register_naia_walkthrough_tools()

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
  local prefix = "For the current walkthrough step:\n"
  local suffix = trim(opts.args or "")
  local msg = prefix

  if suffix ~= "" then
    msg = msg .. suffix
  end

  vim.api.nvim_cmd({
    cmd = "AIMessage",
    args = { msg },
  }, {})
end, {
  nargs = "*",
  desc = "Send a walkthrough-step prompt via AIMessage",
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
  export_walkthrough(opts.args, opts.bang)
end, {
  bang = true,
  nargs = 1,
  complete = "file",
  desc = "Export portable walkthrough JSON (! overwrites)",
})

create_or_replace_user_command("AIWalkImport", function(opts)
  import_walkthrough(opts.args)
end, {
  nargs = 1,
  complete = "file",
  desc = "Load portable JSON or a trusted legacy YAML walkthrough",
})

create_or_replace_user_command("AIWalkLoad", function(opts)
  import_walkthrough(opts.args)
end, { nargs = 1, complete = "file", desc = "Load a walkthrough (alias for AIWalkImport)" })

vim.api.nvim_create_autocmd("User", {
  group = walkthrough_augroup,
  pattern = "AIVoicePlaybackStarted",
  callback = function(event)
    if not playback_state.active or not playback_state.waiting_for_voice then
      return
    end

    playback_state.voice_started = true
    playback_state.voice_request_id = event.data and event.data.request_id
  end,
})

vim.api.nvim_create_autocmd("User", {
  group = walkthrough_augroup,
  pattern = "AIVoicePlaybackStopped",
  callback = function(event)
    if not playback_state.active or not playback_state.waiting_for_voice or not playback_state.voice_started then
      return
    end
    local request_id = event.data and event.data.request_id
    if request_id and playback_state.voice_request_id and request_id ~= playback_state.voice_request_id then return end

    playback_state.waiting_for_voice = false
    playback_state.voice_started = false
    if playback_state.after_narration then playback_state.after_narration() end
  end,
})

vim.api.nvim_create_autocmd("ColorScheme", {
  group = walkthrough_augroup,
  callback = set_walkthrough_highlight,
})
end

return { setup = setup }
