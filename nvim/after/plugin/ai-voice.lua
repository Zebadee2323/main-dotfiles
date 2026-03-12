local voice_name = "en_US-hfc_female-medium"
local voices_dir = vim.fn.expand("~/.config/dotfiles/nvim/after/ai_voices")
local current_tts_job = nil
local current_play_job = nil
local current_wav_path = nil
local current_summary_chat = nil
local speech_request_id = 0
local ai_voice_codecompanion_enabled = false
local cc_group = vim.api.nvim_create_augroup("CodeCompanionHooks", { clear = true })
local summary_prompt = table.concat({
  "Summarize the following assistant response to be passed to a Text-To-Speech model.",
  "The summary should always be from the point of view of the assistant and sound casual.",
  "Try to keep it quite short, only 3-4 sentences max.",
  "Avoid including long file paths, commands or code.",
  "Original response:",
}, " ")

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

local function stop_audio_jobs()
  if current_tts_job and vim.fn.jobwait({ current_tts_job }, 0)[1] == -1 then
    vim.fn.jobstop(current_tts_job)
  end
  current_tts_job = nil

  if current_play_job and vim.fn.jobwait({ current_play_job }, 0)[1] == -1 then
    vim.fn.jobstop(current_play_job)
  end
  current_play_job = nil
  cleanup_current_wav()
end

local function cancel_summary_chat()
  if current_summary_chat then
    pcall(function()
      if current_summary_chat.current_request then
        current_summary_chat:stop()
      end
      current_summary_chat:close()
    end)
    current_summary_chat = nil
  end
end

local function prepare_speech_request(opts)
  opts = opts or {}
  speech_request_id = speech_request_id + 1

  if opts.cancel_summary then
    cancel_summary_chat()
  end

  stop_audio_jobs()

  return speech_request_id
end

local function ai_voice_stop()
  prepare_speech_request({ cancel_summary = true })
end

local function ai_voice_speak(message, opts)
  message = vim.trim(message or "")
  if message == "" then
    vim.notify("AIVoice requires a message", vim.log.levels.ERROR)
    return
  end

  local request_id = prepare_speech_request({
    cancel_summary = not (opts and opts.keep_summary),
  })
  local wav_path = vim.fn.tempname() .. ".wav"
  local cmd = {
    "python3",
    "-m",
    "piper",
    "-m",
    voice_name,
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
          return
        end

        local play_job = vim.fn.jobstart({ "afplay", wav_path }, {
          on_exit = function()
            if speech_request_id == request_id then
              current_play_job = nil
              cleanup_current_wav()
            else
              pcall(vim.fn.delete, wav_path)
            end
          end,
        })

        if play_job <= 0 then
          vim.notify("Generated audio at " .. wav_path .. " but failed to start afplay", vim.log.levels.WARN)
          pcall(vim.fn.delete, wav_path)
          return
        end

        current_play_job = play_job
        current_wav_path = wav_path
      end)
    end,
  })

  if job_id <= 0 then
    vim.notify("Failed to start piper", vim.log.levels.ERROR)
    return
  end

  current_tts_job = job_id
  vim.fn.chansend(job_id, { message, "" })
  vim.fn.chanclose(job_id, "stdin")
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

local function get_latest_assistant_response(chat)
  local codecompanion_config = require("codecompanion.config")

  for i = #chat.messages, 1, -1 do
    local message = chat.messages[i]

    if message.role == codecompanion_config.constants.LLM_ROLE and type(message.content) == "string" then
      local content = vim.trim(message.content)

      if content ~= "" and not (message.tools and message.tools.calls) then
        local cleaned = strip_markdown(content)

        if cleaned ~= "" then
          return cleaned
        end
      end
    end
  end
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

  return "openai/gpt-5.1-codex-mini"
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

local function make_fallback_summary(response)
  local summary = response:gsub("%s+", " ")
  local sentences = {}

  for sentence in summary:gmatch("[^.!?]+[.!?]?") do
    sentence = vim.trim(sentence)

    if sentence ~= "" then
      table.insert(sentences, sentence)
    end

    if #sentences == 3 then
      break
    end
  end

  summary = table.concat(sentences, " ")

  if summary == "" then
    summary = vim.trim(response)
  end

  if #summary > 280 then
    summary = vim.trim(summary:sub(1, 277)) .. "..."
  end

  return summary
end

local function summarize_and_speak_response(chat, response)
  local codecompanion = require("codecompanion")
  local chat_helpers = require("codecompanion.interactions.chat.helpers")
  local request_id = prepare_speech_request({ cancel_summary = true })
  local fallback_summary = make_fallback_summary(response)
  local summary_params = build_summary_chat_params(chat)
  local summary_model_name = get_summary_model_name() or get_chat_model(chat)

  local summary_chat = codecompanion.chat({
    auto_submit = false,
    hidden = true,
    mcp_servers = "none",
    tools = "none",
    params = summary_params,
    messages = {
      {
        role = "user",
        content = summary_prompt .. "\n\n" .. response,
      },
    },
    callbacks = {
      on_created = function(summary_result_chat)
        if summary_result_chat.adapter.type == "acp" then
          if summary_model_name then
            summary_result_chat.adapter.defaults = summary_result_chat.adapter.defaults or {}
            summary_result_chat.adapter.defaults.model = summary_model_name
          end

          chat_helpers.create_acp_connection(summary_result_chat)

          if summary_model_name and summary_result_chat.acp_connection then
            summary_result_chat:change_model({ model = summary_model_name })
          end
        end

        summary_result_chat:submit()
      end,
      on_completed = function(summary_result_chat)
        if current_summary_chat == summary_result_chat then
          current_summary_chat = nil
        end

        if speech_request_id ~= request_id then
          summary_result_chat:close()
          return
        end

        local summary = get_latest_assistant_response(summary_result_chat)

        if summary and summary ~= "" then
          vim.schedule(function()
            ai_voice_speak(summary, { keep_summary = true })
          end)
        else
          vim.schedule(function()
            ai_voice_speak(fallback_summary, { keep_summary = true })
          end)
        end

        summary_result_chat:close()
      end,
      on_cancelled = function(summary_result_chat)
        if current_summary_chat == summary_result_chat then
          current_summary_chat = nil
        end

        summary_result_chat:close()
      end,
    },
  })

  if not summary_chat then
    ai_voice_speak(fallback_summary)
    return
  end

  current_summary_chat = summary_chat
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

  summarize_and_speak_response(chat, response)
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

      if not chat or chat.hidden then
        return
      end

      chat:add_callback("on_completed", function(current_chat)
        if not ai_voice_codecompanion_enabled then
          return
        end

        local response = get_latest_assistant_response(current_chat)

        if response then
          summarize_and_speak_response(current_chat, response)
        end
      end)
    end,
  })
end

attach_codecompanion_voice_hook()

vim.api.nvim_create_user_command("AIVoiceInstall", function()
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

vim.api.nvim_create_user_command("AIVoice", function(opts)
  ai_voice_speak(opts.args)
end, {
  nargs = "+",
  desc = "Speak a message with piper",
})

vim.api.nvim_create_user_command("AIVoiceStop", function()
  ai_voice_stop()
end, {
  desc = "Stop current piper audio playback",
})

vim.api.nvim_create_user_command("AIVoiceRange", function(opts)
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

vim.api.nvim_create_user_command("AIVoiceSummary", function()
  summarize_last_codecompanion_response()
end, {
  desc = "Summarize the latest CodeCompanion reply and speak it",
})

vim.api.nvim_create_user_command("AIToggleVoice", function()
  toggle_ai_voice_codecompanion()
end, {
  desc = "Toggle AI voice for CodeCompanion responses",
})

vim.api.nvim_create_user_command("AIEnableVoice", function()
  set_ai_voice_codecompanion_enabled(true)
end, {
  desc = "Enable AI voice for CodeCompanion responses",
})

vim.api.nvim_create_user_command("AIDisableVoice", function()
  set_ai_voice_codecompanion_enabled(false)
end, {
  desc = "Disable AI voice for CodeCompanion responses",
})
