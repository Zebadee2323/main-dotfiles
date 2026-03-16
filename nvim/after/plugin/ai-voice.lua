local voice_name = "cori-high"
local voices_dir = vim.fn.expand("~/.config/dotfiles/nvim/after/ai_voices")
local summary_model_name = "openai/gpt-5.2/low"
local piper_length_scale = 1.0
local piper_noise_scale = 0.2
local piper_noise_w = 0.2
local piper_sentence_silence = 0.5
local ai_voice_robot_mode = true
local robot_voice_filter = table.concat({
  -- split into 4 parallel signals: dry, body, ai, extra_chorus
  "asplit=4[dry][d1][d2][d3]",

  -- body layer (micro delay adds fullness)
  "[d1]adelay=22|22,volume=0.78[body]",

  -- AI synthetic layer (stronger chorus + small delay + light tremolo)
  "[d2]chorus=0.78:0.98:50|68|82:0.38|0.32|0.26:0.38|0.44|0.50:2.5|2.8|3.3,"
    .. "tremolo=6:0.05,adelay=12|12,volume=0.68[ai]",

  -- extra chorus/widening branch (creates lushness without pitch artifacts)
  "[d3]chorus=0.65:0.9:32|48:0.30|0.24:0.30|0.36:1.8|2.1,volume=0.62[wide]",

  -- mix layers (positional amix)
  "[dry][body][ai][wide]amix=4:weights=1 0.82 0.66 0.6[mix]",

  -- tone shaping after mix
  "[mix]highpass=f=80",
  "equalizer=f=140:t=q:w=1.2:g=3.2",    -- low-mid warmth
  "equalizer=f=3200:t=q:w=1:g=2.8",    -- presence
  "equalizer=f=6000:t=q:w=1:g=1.0",    -- sheen

  -- glue compression and mild de-harsh
  "acompressor=threshold=-20dB:ratio=2.4:attack=12:release=110",
  "equalizer=f=4500:t=q:w=2:g=-1.0",

  -- final level and safety
  "volume=5dB",
  "alimiter=limit=0.92",
}, ",")

local current_tts_job = nil
local current_effect_job = nil
local current_play_job = nil
local current_wav_path = nil
local current_summary_chat = nil
local current_summary_chat_key = nil
local current_acknowledgement_chat = nil
local current_acknowledgement_chat_key = nil
local speech_request_id = 0
local speech_queue = {}
local pending_acknowledgement_request_id = nil
local pending_summary_request_id = nil
local pending_summary_message = nil
local ai_voice_codecompanion_enabled = true
local ai_voice_speak
local queue_ai_voice_speak
local get_latest_assistant_response
local ai_voice_hook_generation = (vim.g.ai_voice_hook_generation or 0) + 1
local cc_group = vim.api.nvim_create_augroup("CodeCompanionHooks", { clear = true })
vim.g.ai_voice_hook_generation = ai_voice_hook_generation
local summary_prompt = table.concat({
  "Task: rewrite the assistant response as short spoken audio for Text-to-speech.",
  "Rules:",
  "- Write as the assistant, in first person when natural.",
  "- Keep only the key outcome, decision, or next step.",
  "- Maximum 3 short sentences.",
  "- Use plain spoken English. Remove markdown, code, commands, and long file paths.",
  "- If useful, address the user as Ollie.",
  "- Output only the final spoken text.",
  "Assistant response:",
}, " ")
local acknowledgement_prompt = table.concat({
  "Task: write a short spoken acknowledgement for Text-to-speech.",
  "Rules:",
  "- Read the latest user request and extract the users intent.",
  "- If a previous assistant response is provided, use it only as supporting context.",
  --"- Sound casual and clear.",
  "- Maximum 2 short sentences.",
  "- Do not ask follow-up questions or answer their request; just give a specific, confident acknowledgement that you will act on the request.",
  "- Avoid reading out long file paths or code.",
  "- If useful, address the user as Ollie.",
  "- Output only the final spoken text.",
  "Conversation context:",
}, " ")
local ai_voice_test_paragraph = (function()
  local text = [[
Good evening, sir. All workshop systems are stable, your calendar has been reduced to the essentials, and the suit diagnostics are comfortably within tolerance.
I have filtered the usual noise from your inbox, flagged the genuinely urgent items, and left the dramatic ones for your amusement.
Whenever you are ready, I can assist with the next experiment, the next idea, or the next spectacularly irresponsible decision.
]]

  return vim.trim(text):gsub("%s+", " ")
end)()

local function collect_lines(target, data)
  if not data then
    return
  end

  for _, line in ipairs(data) do
    if line ~= "" then
      table.insert(target, line)
    end
  end
end

local function cleanup_current_wav()
  if current_wav_path then
    pcall(vim.fn.delete, current_wav_path)
    current_wav_path = nil
  end
end

local function clear_speech_queue()
  speech_queue = {}
end

local function has_active_audio_jobs()
  return current_tts_job ~= nil or current_effect_job ~= nil or current_play_job ~= nil
end

local function stop_audio_jobs()
  if current_tts_job and vim.fn.jobwait({ current_tts_job }, 0)[1] == -1 then
    vim.fn.jobstop(current_tts_job)
  end
  current_tts_job = nil

  if current_effect_job and vim.fn.jobwait({ current_effect_job }, 0)[1] == -1 then
    vim.fn.jobstop(current_effect_job)
  end
  current_effect_job = nil

  if current_play_job and vim.fn.jobwait({ current_play_job }, 0)[1] == -1 then
    vim.fn.jobstop(current_play_job)
  end
  current_play_job = nil
  cleanup_current_wav()
end

local function cancel_hidden_chat(chat)
  if chat then
    pcall(function()
      if chat.current_request then
        chat:stop()
      end
      chat:close()
    end)
  end
end

local function stop_hidden_chat(chat)
  if chat then
    pcall(function()
      if chat.current_request then
        chat:stop()
      end
    end)
  end
end

local function reset_hidden_chat(chat)
  if not chat then
    return
  end

  chat.messages = {}
  chat.header_line = 1
  chat._ai_voice_request_id = nil
  chat._ai_voice_request_text = nil
  chat._ai_voice_source_text = nil

  if vim.api.nvim_buf_is_valid(chat.bufnr) then
    pcall(vim.api.nvim_buf_set_lines, chat.bufnr, 0, -1, false, {})
  end
end

local function close_acknowledgement_chat()
  if current_acknowledgement_chat then
    local acknowledgement_chat = current_acknowledgement_chat
    current_acknowledgement_chat = nil
    current_acknowledgement_chat_key = nil

    cancel_hidden_chat(acknowledgement_chat)
  end
end

local function close_summary_chat()
  if current_summary_chat then
    local summary_chat = current_summary_chat
    current_summary_chat = nil
    current_summary_chat_key = nil

    cancel_hidden_chat(summary_chat)
  end
end

local function acknowledgement_chat_key(params, model_name)
  params = params or {}

  return table.concat({
    params.adapter or "",
    params.model or "",
    params.command or "",
    model_name or "",
  }, "\0")
end

local function cancel_summary_chat()
  if current_summary_chat then
    stop_hidden_chat(current_summary_chat)
    reset_hidden_chat(current_summary_chat)
  end
end

local function cancel_acknowledgement_chat()
  if current_acknowledgement_chat then
    stop_hidden_chat(current_acknowledgement_chat)
    reset_hidden_chat(current_acknowledgement_chat)
  end
end

local function close_chat(chat)
  if not chat then
    return
  end

  pcall(function()
    chat:close()
  end)
end

local function hidden_chat_supports_reuse(params)
  return true
end

local function prepare_speech_request(opts)
  opts = opts or {}
  speech_request_id = speech_request_id + 1

  if opts.cancel_acknowledgement then
    cancel_acknowledgement_chat()
  end

  if opts.cancel_summary then
    cancel_summary_chat()
  end

  if opts.clear_queue then
    clear_speech_queue()
  end

  pending_acknowledgement_request_id = nil
  pending_summary_request_id = nil
  pending_summary_message = nil

  stop_audio_jobs()

  return speech_request_id
end

local function maybe_start_queued_speech()
  if has_active_audio_jobs() or #speech_queue == 0 then
    return
  end

  local next_item = table.remove(speech_queue, 1)
  if not next_item then
    return
  end

  ai_voice_speak(next_item.message, vim.tbl_extend("force", next_item.opts or {}, {
    keep_queue = true,
  }))
end

local function ai_voice_stop()
  prepare_speech_request({ cancel_acknowledgement = true, cancel_summary = true, clear_queue = true })
end

local function set_ai_voice_robot_mode(enabled)
  ai_voice_robot_mode = enabled
  vim.notify("AI voice robot mode is now " .. (enabled and "enabled" or "disabled"), vim.log.levels.INFO)
end

local function toggle_ai_voice_robot_mode()
  set_ai_voice_robot_mode(not ai_voice_robot_mode)
end

local function start_audio_playback(wav_path, request_id)
  local play_job = vim.fn.jobstart({ "afplay", wav_path }, {
    on_exit = function()
      if speech_request_id == request_id then
        current_play_job = nil
        cleanup_current_wav()
        maybe_start_queued_speech()
      else
        pcall(vim.fn.delete, wav_path)
      end
    end,
  })

  if play_job <= 0 then
    vim.notify("Generated audio at " .. wav_path .. " but failed to start afplay", vim.log.levels.WARN)
    pcall(vim.fn.delete, wav_path)
    maybe_start_queued_speech()
    return
  end

  current_play_job = play_job
  current_wav_path = wav_path
end

local function maybe_apply_robot_effects(wav_path, request_id)
  if not ai_voice_robot_mode then
    start_audio_playback(wav_path, request_id)
    return
  end

  if vim.fn.executable("ffmpeg") ~= 1 then
    vim.notify("Robot mode requires ffmpeg; playing original audio", vim.log.levels.WARN)
    start_audio_playback(wav_path, request_id)
    return
  end

  local processed_wav_path = vim.fn.tempname() .. ".wav"
  local stderr = {}
  local effect_job = vim.fn.jobstart({
    "ffmpeg",
    "-y",
    "-i",
    wav_path,
    "-af",
    robot_voice_filter,
    processed_wav_path,
  }, {
    stderr_buffered = true,
    on_stderr = function(_, data)
      collect_lines(stderr, data)
    end,
    on_exit = function(_, code)
      vim.schedule(function()
        current_effect_job = nil

        if speech_request_id ~= request_id then
          pcall(vim.fn.delete, wav_path)
          pcall(vim.fn.delete, processed_wav_path)
          return
        end

        if code ~= 0 then
          local msg = #stderr > 0 and table.concat(stderr, "\n") or ("ffmpeg exited with code " .. code)
          vim.notify("Robot mode failed, playing original audio:\n" .. msg, vim.log.levels.WARN)
          pcall(vim.fn.delete, processed_wav_path)
          start_audio_playback(wav_path, request_id)
          return
        end

        pcall(vim.fn.delete, wav_path)
        start_audio_playback(processed_wav_path, request_id)
      end)
    end,
  })

  if effect_job <= 0 then
    vim.notify("Failed to start ffmpeg for robot mode; playing original audio", vim.log.levels.WARN)
    pcall(vim.fn.delete, processed_wav_path)
    start_audio_playback(wav_path, request_id)
    return
  end

  current_effect_job = effect_job
end

ai_voice_speak = function(message, opts)
  message = vim.trim(message or "")
  if message == "" then
    vim.notify("AIVoice requires a message", vim.log.levels.ERROR)
    return
  end

  local request_id = opts and opts.request_id

  if not request_id and not (opts and opts.keep_queue) then
    clear_speech_queue()
  end

  if not request_id then
    request_id = prepare_speech_request({
      cancel_summary = not (opts and opts.keep_summary),
      cancel_acknowledgement = not (opts and opts.keep_summary),
    })
  end

  local wav_path = vim.fn.tempname() .. ".wav"
  local cmd = {
    "python3",
    "-m",
    "piper",
    "-m",
    voice_name,
    "--length-scale",
    tostring(piper_length_scale),
    "--noise-scale",
    tostring(piper_noise_scale),
    "--noise-w",
    tostring(piper_noise_w),
    "--sentence-silence",
    tostring(piper_sentence_silence),
    "--data-dir",
    voices_dir,
    "-f",
    wav_path,
  }

  local stderr = {}
  local job_id = vim.fn.jobstart(cmd, {
    stderr_buffered = true,
    on_stderr = function(_, data)
      collect_lines(stderr, data)
    end,
    on_exit = function(_, code)
      vim.schedule(function()
        if speech_request_id ~= request_id then
          pcall(vim.fn.delete, wav_path)
          return
        end

        current_tts_job = nil

        if code ~= 0 then
          local msg = #stderr > 0 and table.concat(stderr, "\n") or ("piper exited with code " .. code)
          vim.notify(msg, vim.log.levels.ERROR)
          maybe_start_queued_speech()
          return
        end

        maybe_apply_robot_effects(wav_path, request_id)
      end)
    end,
  })

  if job_id <= 0 then
    vim.notify("Failed to start piper", vim.log.levels.ERROR)
    maybe_start_queued_speech()
    return
  end

  current_tts_job = job_id
  vim.fn.chansend(job_id, message .. "\n")
  vim.fn.chanclose(job_id, "stdin")
end

queue_ai_voice_speak = function(message, opts)
  message = vim.trim(message or "")
  if message == "" then
    return
  end

  if has_active_audio_jobs() then
    table.insert(speech_queue, {
      message = message,
      opts = opts,
    })
    return
  end

  ai_voice_speak(message, vim.tbl_extend("force", opts or {}, {
    keep_queue = true,
  }))
end

local function strip_markdown(text)
  local cleaned = text

  cleaned = cleaned:gsub("\r\n", "\n")
  cleaned = cleaned:gsub("```+[%w_-]*\n", "")
  cleaned = cleaned:gsub("```+", "")
  cleaned = cleaned:gsub("`([^`]+)`", "%1")
  cleaned = cleaned:gsub("!%[([^%]]*)%]%([^%)]+%)", "%1")
  cleaned = cleaned:gsub("%[([^%]]+)%]%([^%)]+%)", "%1")

  local lines = vim.split(cleaned, "\n", { plain = true })

  for i, line in ipairs(lines) do
    line = line:gsub("^%s*>%s?", "")
    line = line:gsub("^%s*#+%s*", "")
    line = line:gsub("^%s*[-*+]%s+", "")
    line = line:gsub("^%s*%d+[.)]%s+", "")
    line = line:gsub("%*%*([^*]+)%*%*", "%1")
    line = line:gsub("__([^_]+)__", "%1")
    line = line:gsub("%*([^*]+)%*", "%1")
    line = line:gsub("_([^_]+)_", "%1")
    line = line:gsub("~~([^~]+)~~", "%1")
    lines[i] = vim.trim(line)
  end

  cleaned = table.concat(lines, "\n")
  cleaned = cleaned:gsub("\n%s*\n%s*\n+", "\n\n")

  return vim.trim(cleaned)
end

get_latest_assistant_response = function(chat)
  local codecompanion_config = require("codecompanion.config")
  local llm_role = codecompanion_config.constants.LLM_ROLE

  for i = #chat.messages, 1, -1 do
    local message = chat.messages[i]

    if message.role == llm_role then
      local content = message.content

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

      if type(content) == "string" then
        content = vim.trim(content)

        if content ~= "" and not (message.tools and message.tools.calls) then
          local cleaned = strip_markdown(content)

          if cleaned ~= "" then
            return cleaned
          end
        end
      end
    end
  end
end

local function get_latest_user_request(chat)
  local codecompanion_config = require("codecompanion.config")
  local user_role = codecompanion_config.constants.USER_ROLE

  for i = #chat.messages, 1, -1 do
    local message = chat.messages[i]

    if message.role == user_role then
      local content = message.content

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

      if type(content) == "string" then
        content = vim.trim(content)

        if content ~= "" then
          local cleaned = strip_markdown(content)

          if cleaned ~= "" then
            return cleaned
          end
        end
      end
    end
  end
end

local function build_acknowledgement_input(request, previous_response)
  local parts = {}

  if previous_response and previous_response ~= "" then
    table.insert(parts, "Previous assistant response:\n" .. previous_response)
  end

  table.insert(parts, "Latest user request:\n" .. request)

  return acknowledgement_prompt .. "\n\n" .. table.concat(parts, "\n\n")
end

local function flush_pending_summary_for_request(request_id)
  if pending_summary_request_id == request_id and pending_summary_message then
    local queued_summary = pending_summary_message
    pending_summary_request_id = nil
    pending_summary_message = nil

    vim.schedule(function()
      queue_ai_voice_speak(queued_summary, { keep_summary = true, request_id = request_id })
    end)
  end
end

local function create_acknowledgement_chat(summary_params, summary_model_name)
  local codecompanion = require("codecompanion")
  local chat_helpers = require("codecompanion.interactions.chat.helpers")

  return codecompanion.chat({
    auto_submit = false,
    hidden = true,
    mcp_servers = "none",
    tools = "none",
    params = summary_params,
    messages = {},
    callbacks = {
      on_created = function(ack_chat)
        if ack_chat.adapter.type == "acp" then
          if summary_model_name then
            ack_chat.adapter.defaults = ack_chat.adapter.defaults or {}
            ack_chat.adapter.defaults.model = summary_model_name
          end

          chat_helpers.create_acp_connection(ack_chat)

          if summary_model_name and ack_chat.acp_connection then
            ack_chat:change_model({ model = summary_model_name })
          end
        end
      end,
      on_completed = function(ack_chat)
        local request_id = ack_chat._ai_voice_request_id

        if speech_request_id ~= request_id then
          reset_hidden_chat(ack_chat)
          return
        end

        local summary = get_latest_assistant_response(ack_chat)
        local acknowledgement = summary and summary ~= "" and summary or ack_chat._ai_voice_request_text

        pending_acknowledgement_request_id = nil

        vim.schedule(function()
          queue_ai_voice_speak(acknowledgement, { keep_summary = true, request_id = request_id })

          if pending_summary_request_id == request_id and pending_summary_message then
            queue_ai_voice_speak(pending_summary_message, { keep_summary = true, request_id = request_id })
            pending_summary_request_id = nil
            pending_summary_message = nil
          end
        end)

        if ack_chat._ai_voice_reusable then
          reset_hidden_chat(ack_chat)
        elseif current_acknowledgement_chat == ack_chat then
          close_acknowledgement_chat()
        else
          cancel_hidden_chat(ack_chat)
        end
      end,
      on_cancelled = function(ack_chat)
        local request_id = ack_chat._ai_voice_request_id

        if pending_acknowledgement_request_id == request_id then
          pending_acknowledgement_request_id = nil
        end

        flush_pending_summary_for_request(request_id)

        if ack_chat._ai_voice_reusable then
          reset_hidden_chat(ack_chat)
        elseif current_acknowledgement_chat == ack_chat then
          close_acknowledgement_chat()
        else
          cancel_hidden_chat(ack_chat)
        end
      end,
    },
  })
end

local function get_or_create_acknowledgement_chat(chat, summary_params, summary_model_name)
  local can_reuse = hidden_chat_supports_reuse(summary_params)

  if not can_reuse then
    close_acknowledgement_chat()
    local acknowledgement_chat = create_acknowledgement_chat(summary_params, summary_model_name)
    if acknowledgement_chat then
      acknowledgement_chat._ai_voice_reusable = false
      current_acknowledgement_chat = acknowledgement_chat
      current_acknowledgement_chat_key = nil
    end
    return acknowledgement_chat
  end

  local key = acknowledgement_chat_key(summary_params, summary_model_name)

  if current_acknowledgement_chat then
    local invalid = not vim.api.nvim_buf_is_valid(current_acknowledgement_chat.bufnr)
    if invalid or current_acknowledgement_chat_key ~= key then
      close_acknowledgement_chat()
    end
  end

  if current_acknowledgement_chat then
    return current_acknowledgement_chat
  end

  local acknowledgement_chat = create_acknowledgement_chat(summary_params, summary_model_name)
  if not acknowledgement_chat then
    return nil
  end

  current_acknowledgement_chat = acknowledgement_chat
  current_acknowledgement_chat_key = key
  acknowledgement_chat._ai_voice_reusable = true

  return acknowledgement_chat
end

local function create_summary_chat(summary_params, summary_model_name)
  local codecompanion = require("codecompanion")
  local chat_helpers = require("codecompanion.interactions.chat.helpers")

  return codecompanion.chat({
    auto_submit = false,
    hidden = true,
    mcp_servers = "none",
    tools = "none",
    params = summary_params,
    messages = {},
    callbacks = {
      on_created = function(summary_chat)
        if summary_chat.adapter.type == "acp" then
          if summary_model_name then
            summary_chat.adapter.defaults = summary_chat.adapter.defaults or {}
            summary_chat.adapter.defaults.model = summary_model_name
          end

          chat_helpers.create_acp_connection(summary_chat)

          if summary_model_name and summary_chat.acp_connection then
            summary_chat:change_model({ model = summary_model_name })
          end
        end
      end,
      on_completed = function(summary_chat)
        local request_id = summary_chat._ai_voice_request_id
        local source_response = summary_chat._ai_voice_source_text

        if speech_request_id ~= request_id then
          reset_hidden_chat(summary_chat)
          return
        end

        local summary = get_latest_assistant_response(summary_chat)
        local spoken_summary = summary and summary ~= "" and summary or source_response

        if pending_acknowledgement_request_id == request_id then
          pending_summary_request_id = request_id
          pending_summary_message = spoken_summary
        else
          vim.schedule(function()
            queue_ai_voice_speak(spoken_summary, { keep_summary = true, request_id = request_id })
          end)
        end

        if summary_chat._ai_voice_reusable then
          reset_hidden_chat(summary_chat)
        elseif current_summary_chat == summary_chat then
          close_summary_chat()
        else
          cancel_hidden_chat(summary_chat)
        end
      end,
      on_cancelled = function(summary_chat)
        if summary_chat._ai_voice_reusable then
          reset_hidden_chat(summary_chat)
        elseif current_summary_chat == summary_chat then
          close_summary_chat()
        else
          cancel_hidden_chat(summary_chat)
        end
      end,
    },
  })
end

local function get_or_create_summary_chat(summary_params, summary_model_name)
  local can_reuse = hidden_chat_supports_reuse(summary_params)

  if not can_reuse then
    close_summary_chat()
    local summary_chat = create_summary_chat(summary_params, summary_model_name)
    if summary_chat then
      summary_chat._ai_voice_reusable = false
      current_summary_chat = summary_chat
      current_summary_chat_key = nil
    end
    return summary_chat
  end

  local key = acknowledgement_chat_key(summary_params, summary_model_name)

  if current_summary_chat then
    local invalid = not vim.api.nvim_buf_is_valid(current_summary_chat.bufnr)
    if invalid or current_summary_chat_key ~= key then
      close_summary_chat()
    end
  end

  if current_summary_chat then
    return current_summary_chat
  end

  local summary_chat = create_summary_chat(summary_params, summary_model_name)
  if not summary_chat then
    return nil
  end

  current_summary_chat = summary_chat
  current_summary_chat_key = key
  summary_chat._ai_voice_reusable = true

  return summary_chat
end

local function get_target_chat()
  local ok, codecompanion = pcall(require, "codecompanion")

  if not ok then
    return nil, "CodeCompanion is not available"
  end

  local current_ok, current_chat = pcall(codecompanion.buf_get_chat, 0)

  if current_ok and current_chat then
    return current_chat
  end

  local last_chat = codecompanion.last_chat()

  if last_chat then
    return last_chat
  end

  return nil, "No CodeCompanion chat found"
end

local function get_chat_model(chat)
  if not (chat and chat.adapter) then
    return nil
  end

  if chat.adapter.type == "acp" and chat.acp_connection then
    local models = chat.acp_connection:get_models()

    if models and models.currentModelId then
      return models.currentModelId
    end
  end

  if chat.adapter.schema and chat.adapter.schema.model then
    return chat.adapter.schema.model.default
  end

  if chat.adapter.defaults then
    return chat.adapter.defaults.model
  end
end

local function get_chat_command(chat)
  if not (chat and chat.adapter and chat.adapter.type == "acp" and chat.adapter.commands) then
    return nil
  end

  local selected = chat.adapter.commands.selected

  if not selected then
    return nil
  end

  for name, command in pairs(chat.adapter.commands) do
    if name ~= "selected" and command == selected then
      return name
    end
  end

  return "default"
end

local function get_summary_model_name()
  if vim.g.ai_voice_summary_model_name ~= nil then
    return vim.g.ai_voice_summary_model_name
  end

  if vim.g.ai_voice_summary_model ~= nil then
    return vim.g.ai_voice_summary_model
  end

  return summary_model_name
end

local function build_summary_chat_params(chat)
  local config_ok, codecompanion_config = pcall(require, "codecompanion.config")

  if config_ok then
    local summary_adapter_name = vim.g.ai_voice_summary_adapter_name or "opencode"
    local acp_adapter = codecompanion_config.adapters.acp and codecompanion_config.adapters.acp[summary_adapter_name]
    local http_adapter = codecompanion_config.adapters.http and codecompanion_config.adapters.http[summary_adapter_name]
    local has_summary_adapter = acp_adapter or http_adapter
    local is_acp_adapter = acp_adapter ~= nil

    if has_summary_adapter then
      local params = {
        adapter = summary_adapter_name,
      }

      if not is_acp_adapter then
        params.model = get_summary_model_name() or get_chat_model(chat)
      end

      if vim.g.ai_voice_summary_command_name then
        params.command = vim.g.ai_voice_summary_command_name
      end

      return params
    end
  end

  if not (chat and chat.adapter and chat.adapter.name) then
    return nil
  end

  local params = {
    adapter = chat.adapter.name,
  }

  local model_name = get_chat_model(chat)
  if model_name and chat.adapter.type ~= "acp" then
    params.model = model_name
  end

  local command_name = get_chat_command(chat)
  if command_name then
    params.command = command_name
  end

  return params
end

local function summarize_and_speak_response(chat, response, request_id)
  local summary_params = build_summary_chat_params(chat)
  local summary_model_name = get_summary_model_name() or get_chat_model(chat)
  local summary_chat = get_or_create_summary_chat(summary_params, summary_model_name)

  if not summary_chat then
    vim.notify("AI voice summary failed: could not create summary chat", vim.log.levels.ERROR)
    return
  end

  reset_hidden_chat(summary_chat)

  summary_chat._ai_voice_request_id = request_id
  summary_chat._ai_voice_source_text = response
  summary_chat.messages = {
    {
      role = "user",
      content = summary_prompt .. "\n\n" .. response,
    },
  }

  summary_chat.ui:render(summary_chat.buffer_context, summary_chat.messages, {
    stop_context_insertion = true,
  })
  summary_chat:submit()
end

local function acknowledge_and_speak_request(chat, request, request_id)
  local summary_params = build_summary_chat_params(chat)
  local summary_model_name = get_summary_model_name() or get_chat_model(chat)
  local previous_response = get_latest_assistant_response(chat)
  local acknowledgement_input = build_acknowledgement_input(request, previous_response)
  local acknowledgement_chat = get_or_create_acknowledgement_chat(chat, summary_params, summary_model_name)

  if not acknowledgement_chat then
    if pending_acknowledgement_request_id == request_id then
      pending_acknowledgement_request_id = nil
    end

    flush_pending_summary_for_request(request_id)

    vim.notify("AI voice acknowledgement failed: could not create hidden chat", vim.log.levels.ERROR)
    return
  end

  reset_hidden_chat(acknowledgement_chat)

  acknowledgement_chat._ai_voice_request_id = request_id
  acknowledgement_chat._ai_voice_request_text = request
  acknowledgement_chat.messages = {
    {
      role = "user",
      content = acknowledgement_input,
    },
  }

  acknowledgement_chat.ui:render(acknowledgement_chat.buffer_context, acknowledgement_chat.messages, {
    stop_context_insertion = true,
  })
  acknowledgement_chat:submit()
end

local function summarize_last_codecompanion_response()
  local chat, err = get_target_chat()

  if not chat then
    vim.notify(err, vim.log.levels.WARN)
    return
  end

  local response = get_latest_assistant_response(chat)

  if not response then
    vim.notify("No assistant response available to summarize", vim.log.levels.WARN)
    return
  end

  local request_id = prepare_speech_request({
    cancel_acknowledgement = true,
    cancel_summary = true,
    clear_queue = true,
  })

  summarize_and_speak_response(chat, response, request_id)
end

local function set_ai_voice_codecompanion_enabled(enabled)
  ai_voice_codecompanion_enabled = enabled
  vim.notify(
    "AI voice CodeCompanion responses are now " .. (enabled and "enabled" or "disabled"),
    vim.log.levels.INFO
  )
end

local function toggle_ai_voice_codecompanion()
  set_ai_voice_codecompanion_enabled(not ai_voice_codecompanion_enabled)
end

local function create_or_replace_user_command(name, fn, opts)
  pcall(vim.api.nvim_del_user_command, name)
  vim.api.nvim_create_user_command(name, fn, opts)
end

local function attach_ai_voice_to_chat(bufnr, chat)
  if not chat or chat.hidden then
    return
  end

  local ok, attached_generation = pcall(vim.api.nvim_buf_get_var, bufnr, "ai_voice_hook_generation")
  if ok and attached_generation == ai_voice_hook_generation then
    return
  end

  pcall(vim.api.nvim_buf_set_var, bufnr, "ai_voice_hook_generation", ai_voice_hook_generation)

  chat._ai_voice_hook_generation = ai_voice_hook_generation
  local callback_generation = ai_voice_hook_generation

  chat:add_callback("on_completed", function(current_chat)
    if current_chat._ai_voice_hook_generation ~= callback_generation then
      return
    end

    if not ai_voice_codecompanion_enabled then
      return
    end

    local response = get_latest_assistant_response(current_chat)

    if response then
      summarize_and_speak_response(current_chat, response, speech_request_id)
    end
  end)

  chat:add_callback("on_submitted", function(current_chat)
    if current_chat._ai_voice_hook_generation ~= callback_generation then
      return
    end

    if not ai_voice_codecompanion_enabled then
      return
    end

    local request = get_latest_user_request(current_chat)

    if request then
      local request_id = prepare_speech_request({
        cancel_acknowledgement = true,
        cancel_summary = true,
        clear_queue = true,
      })

      pending_acknowledgement_request_id = request_id
      acknowledge_and_speak_request(current_chat, request, request_id)
    end
  end)
end

local function attach_ai_voice_to_existing_chats(codecompanion)
  for _, bufnr in ipairs(vim.api.nvim_list_bufs()) do
    if vim.api.nvim_buf_is_loaded(bufnr) then
      local ok, chat = pcall(codecompanion.buf_get_chat, bufnr)

      if ok and chat then
        attach_ai_voice_to_chat(bufnr, chat)
      end
    end
  end
end

local function attach_codecompanion_voice_hook()
  local ok, codecompanion = pcall(require, "codecompanion")

  if not ok then
    return
  end

  vim.api.nvim_create_autocmd({ "User" }, {
    pattern = "CodeCompanionChatCreated",
    group = cc_group,
    callback = function(args)
      local chat = codecompanion.buf_get_chat(args.data.bufnr)

      attach_ai_voice_to_chat(args.data.bufnr, chat)
    end,
  })

  attach_ai_voice_to_existing_chats(codecompanion)
end

attach_codecompanion_voice_hook()

create_or_replace_user_command("AIVoiceInstall", function()
  vim.fn.mkdir(voices_dir, "p")

  local output = {}
  local job_id = vim.fn.jobstart({
    "python3",
    "-m",
    "piper.download_voices",
    "--download-dir",
    voices_dir,
    voice_name,
  }, {
    stdout_buffered = true,
    stderr_buffered = true,
    on_stdout = function(_, data)
      collect_lines(output, data)
    end,
    on_stderr = function(_, data)
      collect_lines(output, data)
    end,
    on_exit = function(_, code)
      vim.schedule(function()
        if code == 0 then
          vim.notify("Installed " .. voice_name .. " to " .. voices_dir, vim.log.levels.INFO)
          return
        end

        local msg = #output > 0 and table.concat(output, "\n") or ("voice install failed with code " .. code)
        vim.notify(msg, vim.log.levels.ERROR)
      end)
    end,
  })

  if job_id <= 0 then
    vim.notify("Failed to start piper voice install", vim.log.levels.ERROR)
  end
end, {
  desc = "Install the configured piper voice",
})

create_or_replace_user_command("AIVoice", function(opts)
  ai_voice_speak(opts.args)
end, {
  nargs = "+",
  desc = "Speak a message with piper",
})

create_or_replace_user_command("AIVoiceTest", function()
  ai_voice_speak(ai_voice_test_paragraph)
end, {
  desc = "Speak the built-in AI voice test paragraph",
})

create_or_replace_user_command("AIVoiceStop", function()
  ai_voice_stop()
end, {
  desc = "Stop current piper audio playback",
})

create_or_replace_user_command("AIVoiceRange", function(opts)
  local lines = vim.api.nvim_buf_get_lines(0, opts.line1 - 1, opts.line2, false)
  local message = vim.trim(table.concat(lines, "\n"))

  if message == "" then
    vim.notify("AIVoiceRange requires a non-empty range", vim.log.levels.ERROR)
    return
  end

  ai_voice_speak(message)
end, {
  range = true,
  desc = "Speak the selected line range with piper",
})

create_or_replace_user_command("AIVoiceSummary", function()
  summarize_last_codecompanion_response()
end, {
  desc = "Summarize the latest CodeCompanion reply and speak it",
})

create_or_replace_user_command("AIToggleVoice", function()
  toggle_ai_voice_codecompanion()
end, {
  desc = "Toggle AI voice for CodeCompanion responses",
})

create_or_replace_user_command("AIEnableVoice", function()
  set_ai_voice_codecompanion_enabled(true)
end, {
  desc = "Enable AI voice for CodeCompanion responses",
})

create_or_replace_user_command("AIDisableVoice", function()
  set_ai_voice_codecompanion_enabled(false)
end, {
  desc = "Disable AI voice for CodeCompanion responses",
})

create_or_replace_user_command("AIToggleVoiceRobot", function()
  toggle_ai_voice_robot_mode()
end, {
  desc = "Toggle robot mode for AI voice playback",
})

create_or_replace_user_command("AIEnableVoiceRobot", function()
  set_ai_voice_robot_mode(true)
end, {
  desc = "Enable robot mode for AI voice playback",
})

create_or_replace_user_command("AIDisableVoiceRobot", function()
  set_ai_voice_robot_mode(false)
end, {
  desc = "Disable robot mode for AI voice playback",
})
