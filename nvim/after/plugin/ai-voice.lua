local default_voice_name = "en_GB-northern_english_male-medium"
local voice_name = vim.g.ai_voice_name or default_voice_name
local voices_dir = vim.fs.joinpath(vim.fn.stdpath("config"), "after", "ai_voices")
local ai_voice_audio_dir = vim.fn.stdpath("cache") .. "/ai-voice"
local raw_wav_path = ai_voice_audio_dir .. "/latest.wav"
local raw_elevenlabs_audio_path = ai_voice_audio_dir .. "/latest-elevenlabs.mp3"
local processed_wav_path = ai_voice_audio_dir .. "/latest-robot.wav"
local default_ai_voice_tts_mode = "eleven"
local ai_voice_tts_mode = vim.g.ai_voice_tts_mode or default_ai_voice_tts_mode
local piper_length_scale = 1.0
local piper_noise_scale = 0.2
local piper_noise_w = 0.2
local piper_sentence_silence = 0.5
local default_elevenlabs_voice_id = "pYYFYeYrSgj4lcmIMcOL"
local elevenlabs_api_key_env_var = vim.g.ai_voice_elevenlabs_api_key_env_var or "ELEVENLABS_API_KEY"
local elevenlabs_voice_id = vim.g.ai_voice_elevenlabs_voice_id or default_elevenlabs_voice_id
local elevenlabs_model_id = vim.g.ai_voice_elevenlabs_model_id or "eleven_turbo_v2_5"
local elevenlabs_output_format = vim.g.ai_voice_elevenlabs_output_format or "mp3_44100_128"
local default_elevenlabs_speed = 1.0
local elevenlabs_min_speed = 0.7
local elevenlabs_max_speed = 1.2
local elevenlabs_speed = tonumber(vim.g.ai_voice_elevenlabs_speed) or default_elevenlabs_speed
local ai_voice_volume = tonumber(vim.g.ai_voice_volume) or 0.4
local ai_voice_robot_mode = true
local python_host_prog = vim.g.python3_host_prog
if type(python_host_prog) ~= "string" or python_host_prog == "" then
  python_host_prog = "python3"
end
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
local last_spoken_message = nil
local speech_request_id = 0
local speech_queue = {}
local voice_playback_active = false
local ai_voice_enabled = vim.g.ai_voice_enabled ~= false
local ai_voice_speak
local queue_ai_voice_speak
vim.g.ai_voice_enabled = ai_voice_enabled

local function is_ai_voice_enabled()
  return ai_voice_enabled
end

_G.ai_voice_is_enabled = is_ai_voice_enabled
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

local function json_encode(value)
  if vim.json and vim.json.encode then
    return vim.json.encode(value)
  end

  return vim.fn.json_encode(value)
end

local function shell_config_quote(value)
  value = tostring(value or "")
  value = value:gsub("\\", "\\\\"):gsub('"', '\\"')
  return '"' .. value .. '"'
end

local function url_encode(value)
  return tostring(value or ""):gsub("([^%w%-%._~])", function(char)
    return string.format("%%%02X", string.byte(char))
  end)
end

local function normalize_ai_voice_tts_mode(mode)
  mode = vim.trim(tostring(mode or "")):lower()

  if mode == "" then
    return ai_voice_tts_mode
  end

  if mode == "eleven" or mode == "elevenlabs" or mode == "11labs" then
    return "elevenlabs"
  end

  if mode == "piper" then
    return "piper"
  end

  return nil
end

local function normalize_elevenlabs_speed(speed)
  speed = tonumber(speed)

  if not speed or speed ~= speed then
    return nil
  end

  if speed < elevenlabs_min_speed or speed > elevenlabs_max_speed then
    return nil
  end

  return speed
end

ai_voice_tts_mode = normalize_ai_voice_tts_mode(ai_voice_tts_mode) or default_ai_voice_tts_mode
vim.g.ai_voice_tts_mode = ai_voice_tts_mode
elevenlabs_speed = normalize_elevenlabs_speed(elevenlabs_speed) or default_elevenlabs_speed
vim.g.ai_voice_elevenlabs_speed = elevenlabs_speed

local function fire_user_event(pattern, data)
  vim.schedule(function()
    pcall(vim.api.nvim_exec_autocmds, "User", {
      pattern = pattern,
      data = data or {},
    })
  end)
end

local function set_voice_playback_active(active, data)
  if voice_playback_active == active then
    return
  end

  voice_playback_active = active
  fire_user_event(active and "AIVoicePlaybackStarted" or "AIVoicePlaybackStopped", data)
end

local function ensure_ai_voice_audio_dir()
  vim.fn.mkdir(ai_voice_audio_dir, "p")
end

local function clear_speech_queue()
  local cleared = #speech_queue
  speech_queue = {}
  return cleared
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
  set_voice_playback_active(false, { request_id = speech_request_id })
end

local function prepare_speech_request(opts)
  opts = opts or {}
  speech_request_id = speech_request_id + 1

  if opts.clear_queue then
    clear_speech_queue()
  end

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

local function ai_voice_stop(opts)
  opts = opts or {}

  local cleared = clear_speech_queue()
  local had_active_audio = has_active_audio_jobs()

  prepare_speech_request()

  if opts.silent then
    return
  end

  local message
  if had_active_audio then
    message = "Stopped AI voice playback"
  else
    message = "AI voice playback already stopped"
  end

  if cleared > 0 then
    local queued_label = cleared == 1 and "queued message" or "queued messages"
    message = message .. " and cleared " .. cleared .. " " .. queued_label
  end

  vim.notify(message, vim.log.levels.INFO)
end

local function set_ai_voice_robot_mode(enabled)
  ai_voice_robot_mode = enabled
  vim.notify("AI voice robot mode is now " .. (enabled and "enabled" or "disabled"), vim.log.levels.INFO)
end

local function toggle_ai_voice_robot_mode()
  set_ai_voice_robot_mode(not ai_voice_robot_mode)
end

local function set_ai_voice_tts_mode(mode)
  local normalized_mode = normalize_ai_voice_tts_mode(mode)

  if not normalized_mode then
    vim.notify("AI voice mode must be one of: piper, elevenlabs", vim.log.levels.ERROR)
    return false
  end

  ai_voice_tts_mode = normalized_mode
  vim.g.ai_voice_tts_mode = normalized_mode
  vim.notify("AI voice TTS mode is now " .. normalized_mode, vim.log.levels.INFO)
  return true
end

local function set_elevenlabs_voice_id(voice_id)
  voice_id = vim.trim(voice_id or "")

  if voice_id == "" then
    vim.notify("Current ElevenLabs voice ID: " .. elevenlabs_voice_id, vim.log.levels.INFO)
    return true
  end

  elevenlabs_voice_id = voice_id
  vim.g.ai_voice_elevenlabs_voice_id = voice_id
  vim.notify("ElevenLabs voice ID is now " .. voice_id, vim.log.levels.INFO)
  return true
end

local function set_elevenlabs_model_id(model_id)
  model_id = vim.trim(model_id or "")

  if model_id == "" then
    vim.notify("Current ElevenLabs model ID: " .. elevenlabs_model_id, vim.log.levels.INFO)
    return true
  end

  elevenlabs_model_id = model_id
  vim.g.ai_voice_elevenlabs_model_id = model_id
  vim.notify("ElevenLabs model ID is now " .. model_id, vim.log.levels.INFO)
  return true
end

local function set_elevenlabs_speed(speed)
  local normalized_speed = normalize_elevenlabs_speed(speed)

  if not normalized_speed then
    vim.notify(
      "ElevenLabs voice speed must be a number from "
        .. string.format("%.2f", elevenlabs_min_speed)
        .. " to "
        .. string.format("%.2f", elevenlabs_max_speed),
      vim.log.levels.ERROR
    )
    return false
  end

  elevenlabs_speed = normalized_speed
  vim.g.ai_voice_elevenlabs_speed = normalized_speed
  vim.notify("ElevenLabs voice speed is now " .. string.format("%.2f", normalized_speed), vim.log.levels.INFO)
  return true
end

local function start_audio_playback(wav_path, request_id)
  local ffplay_volume = math.floor(ai_voice_volume * 100)
  local play_job = vim.fn.jobstart({
    "ffplay",
    "-nodisp",
    "-autoexit",
    "-loglevel",
    "error",
    "-volume",
    tostring(ffplay_volume),
    wav_path,
  }, {
    on_exit = function()
      set_voice_playback_active(false, { request_id = request_id })

      if speech_request_id == request_id then
        current_play_job = nil
        maybe_start_queued_speech()
      end
    end,
  })

  if play_job <= 0 then
    vim.notify("Generated audio at " .. wav_path .. " but failed to start ffplay", vim.log.levels.WARN)
    maybe_start_queued_speech()
    return
  end

  current_play_job = play_job
  set_voice_playback_active(true, {
    request_id = request_id,
    wav_path = wav_path,
  })
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
          return
        end

        if code ~= 0 then
          local msg = #stderr > 0 and table.concat(stderr, "\n") or ("ffmpeg exited with code " .. code)
          vim.notify("Robot mode failed, playing original audio:\n" .. msg, vim.log.levels.WARN)
          start_audio_playback(wav_path, request_id)
          return
        end

        start_audio_playback(processed_wav_path, request_id)
      end)
    end,
  })

  if effect_job <= 0 then
    vim.notify("Failed to start ffmpeg for robot mode; playing original audio", vim.log.levels.WARN)
    start_audio_playback(wav_path, request_id)
    return
  end

  current_effect_job = effect_job
end

local function synthesize_with_piper(message, request_id)
  local cmd = {
    python_host_prog,
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
    raw_wav_path,
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
          return
        end

        current_tts_job = nil

        if code ~= 0 then
          local msg = #stderr > 0 and table.concat(stderr, "\n") or ("piper exited with code " .. code)
          vim.notify(msg, vim.log.levels.ERROR)
          maybe_start_queued_speech()
          return
        end

        maybe_apply_robot_effects(raw_wav_path, request_id)
      end)
    end,
  })

  if job_id <= 0 then
    vim.notify("Failed to start piper", vim.log.levels.ERROR)
    maybe_start_queued_speech()
    return false
  end

  current_tts_job = job_id
  vim.fn.chansend(job_id, message .. "\n")
  vim.fn.chanclose(job_id, "stdin")
  return true
end

local function synthesize_with_elevenlabs(message, request_id)
  if vim.fn.executable("curl") ~= 1 then
    vim.notify("ElevenLabs voice mode requires curl", vim.log.levels.ERROR)
    maybe_start_queued_speech()
    return false
  end

  local api_key = vim.env[elevenlabs_api_key_env_var]
  if not api_key or api_key == "" then
    vim.notify("ElevenLabs voice mode requires $" .. elevenlabs_api_key_env_var, vim.log.levels.ERROR)
    maybe_start_queued_speech()
    return false
  end

  local payload = {
    text = message,
    model_id = elevenlabs_model_id,
  }

  local voice_settings = {}
  if type(vim.g.ai_voice_elevenlabs_voice_settings) == "table" then
    voice_settings = vim.deepcopy(vim.g.ai_voice_elevenlabs_voice_settings)
  end
  voice_settings.speed = elevenlabs_speed
  payload.voice_settings = voice_settings

  local ok, encoded_payload = pcall(json_encode, payload)
  if not ok then
    vim.notify("Failed to encode ElevenLabs request: " .. tostring(encoded_payload), vim.log.levels.ERROR)
    maybe_start_queued_speech()
    return false
  end

  local payload_path = ai_voice_audio_dir .. "/latest-elevenlabs-payload.json"
  local write_ok, write_err = pcall(vim.fn.writefile, { encoded_payload }, payload_path, "b")
  if not write_ok or write_err ~= 0 then
    vim.notify("Failed to write ElevenLabs request payload", vim.log.levels.ERROR)
    maybe_start_queued_speech()
    return false
  end

  local url = "https://api.elevenlabs.io/v1/text-to-speech/"
    .. url_encode(elevenlabs_voice_id)
    .. "/stream?output_format="
    .. url_encode(elevenlabs_output_format)

  pcall(vim.fn.delete, raw_elevenlabs_audio_path)

  local stderr = {}
  local job_id = vim.fn.jobstart({
    "curl",
    "--fail-with-body",
    "--silent",
    "--show-error",
    "--location",
    "--request",
    "POST",
    url,
    "--header",
    "Content-Type: application/json",
    "--data-binary",
    "@" .. payload_path,
    "--output",
    raw_elevenlabs_audio_path,
    "--config",
    "-",
  }, {
    stderr_buffered = true,
    on_stderr = function(_, data)
      collect_lines(stderr, data)
    end,
    on_exit = function(_, code)
      vim.schedule(function()
        if speech_request_id ~= request_id then
          return
        end

        current_tts_job = nil

        if code ~= 0 then
          local msg = #stderr > 0 and table.concat(stderr, "\n") or ("curl exited with code " .. code)
          local read_ok, error_body = pcall(vim.fn.readfile, raw_elevenlabs_audio_path, "", 8)
          if read_ok and #error_body > 0 then
            msg = msg .. "\n" .. table.concat(error_body, "\n")
          end
          vim.notify("ElevenLabs TTS failed:\n" .. msg, vim.log.levels.ERROR)
          maybe_start_queued_speech()
          return
        end

        maybe_apply_robot_effects(raw_elevenlabs_audio_path, request_id)
      end)
    end,
  })

  if job_id <= 0 then
    vim.notify("Failed to start ElevenLabs TTS request", vim.log.levels.ERROR)
    maybe_start_queued_speech()
    return false
  end

  current_tts_job = job_id
  vim.fn.chansend(job_id, "header = " .. shell_config_quote("xi-api-key: " .. api_key) .. "\n")
  vim.fn.chanclose(job_id, "stdin")
  return true
end

ai_voice_speak = function(message, opts)
  if not ai_voice_enabled then
    return false
  end

  message = vim.trim(message or "")
  if message == "" then
    vim.notify("AIVoice requires a message", vim.log.levels.ERROR)
    return
  end

  last_spoken_message = message

  local request_id = opts and opts.request_id

  if not request_id and not (opts and opts.keep_queue) then
    clear_speech_queue()
  end

  if not request_id then
    request_id = prepare_speech_request()
  end

  ensure_ai_voice_audio_dir()

  if ai_voice_tts_mode == "elevenlabs" then
    return synthesize_with_elevenlabs(message, request_id)
  end

  return synthesize_with_piper(message, request_id)
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

local function replay_last_audio_message()
  if not last_spoken_message then
    vim.notify("No previous AI voice message available to replay", vim.log.levels.WARN)
    return
  end

  ai_voice_speak(last_spoken_message)
end

local function set_ai_voice_enabled(enabled)
  ai_voice_enabled = enabled == true
  vim.g.ai_voice_enabled = ai_voice_enabled
  vim.notify("AI voice is now " .. (ai_voice_enabled and "enabled" or "disabled"), vim.log.levels.INFO)
end

local function set_ai_voice_volume(volume)
  if type(volume) ~= "number" or volume ~= volume or volume < 0 then
    vim.notify("AI voice volume must be a non-negative number", vim.log.levels.ERROR)
    return false
  end

  ai_voice_volume = volume
  vim.g.ai_voice_volume = volume
  vim.notify("AI voice volume is now set to " .. string.format("%.2f", volume), vim.log.levels.INFO)
  return true
end

local function list_ai_voice_names()
  local names = {}

  if vim.fn.isdirectory(voices_dir) ~= 1 then
    return names
  end

  for name, type_ in vim.fs.dir(voices_dir) do
    if type_ == "file" and name:sub(-5) == ".onnx" then
      table.insert(names, name:sub(1, -6))
    end
  end

  table.sort(names)
  return names
end

local function complete_ai_voice_names(arg_lead)
  local matches = {}

  for _, name in ipairs(list_ai_voice_names()) do
    if vim.startswith(name, arg_lead) then
      table.insert(matches, name)
    end
  end

  return matches
end

local function complete_ai_voice_tts_modes(arg_lead)
  local matches = {}

  for _, mode in ipairs({ "piper", "elevenlabs" }) do
    if vim.startswith(mode, arg_lead) then
      table.insert(matches, mode)
    end
  end

  return matches
end

local function set_ai_voice_name(name)
  name = vim.trim(name or "")
  name = name:gsub("%.onnx$", "")

  if name == "" then
    vim.notify("Current AI voice name: " .. voice_name, vim.log.levels.INFO)
    return true
  end

  local available = list_ai_voice_names()
  local available_lookup = {}
  for _, available_name in ipairs(available) do
    available_lookup[available_name] = true
  end

  if not available_lookup[name] then
    local message = "AI voice not found: " .. name
    if #available > 0 then
      message = message .. "\nAvailable voices: " .. table.concat(available, ", ")
    else
      message = message .. "\nNo .onnx voices found in " .. voices_dir
    end
    vim.notify(message, vim.log.levels.ERROR)
    return false
  end

  voice_name = name
  vim.g.ai_voice_name = name
  vim.notify("AI voice name is now " .. voice_name, vim.log.levels.INFO)
  return true
end

local function create_or_replace_user_command(name, fn, opts)
  pcall(vim.api.nvim_del_user_command, name)
  vim.api.nvim_create_user_command(name, fn, opts)
end

vim.keymap.set("n", "[v", "<Cmd>AIVoiceStop<CR>", {
  desc = "Stop AI voice playback",
})

create_or_replace_user_command("AIVoiceInstall", function()
  vim.fn.mkdir(voices_dir, "p")

  local output = {}
  local job_id = vim.fn.jobstart({
    python_host_prog,
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
  queue_ai_voice_speak(opts.args)
end, {
  nargs = "+",
  desc = "Queue a message for AI voice playback",
})

create_or_replace_user_command("AIVoiceInterrupt", function(opts)
  ai_voice_stop({ silent = true })
  queue_ai_voice_speak(opts.args)
end, {
  nargs = "+",
  desc = "Interrupt playback, clear queued speech, and speak a new message",
})

create_or_replace_user_command("AIVoiceTest", function()
  ai_voice_speak(ai_voice_test_paragraph)
end, {
  desc = "Speak the built-in AI voice test paragraph",
})

create_or_replace_user_command("AIVoiceVolume", function(opts)
  local volume = tonumber(opts.args)

  if not volume then
    vim.notify("AIVoiceVolume requires a numeric volume", vim.log.levels.ERROR)
    return
  end

  set_ai_voice_volume(volume)
end, {
  nargs = 1,
  desc = "Set AI voice playback volume",
})

create_or_replace_user_command("AIVoiceName", function(opts)
  set_ai_voice_name(opts.args)
end, {
  nargs = "?",
  complete = complete_ai_voice_names,
  desc = "Set AI voice name from installed .onnx voices",
})

create_or_replace_user_command("AIVoiceMode", function(opts)
  if opts.args == "" then
    vim.notify("Current AI voice TTS mode: " .. ai_voice_tts_mode, vim.log.levels.INFO)
    return
  end

  set_ai_voice_tts_mode(opts.args)
end, {
  nargs = "?",
  complete = complete_ai_voice_tts_modes,
  desc = "Set AI voice TTS mode: piper or elevenlabs",
})

create_or_replace_user_command("AIEnableVoicePiper", function()
  set_ai_voice_tts_mode("piper")
end, {
  desc = "Use local piper for AI voice TTS",
})

create_or_replace_user_command("AIEnableVoiceElevenLabs", function()
  set_ai_voice_tts_mode("elevenlabs")
end, {
  desc = "Use ElevenLabs for AI voice TTS",
})

create_or_replace_user_command("AIVoiceElevenLabsVoiceId", function(opts)
  set_elevenlabs_voice_id(opts.args)
end, {
  nargs = "?",
  desc = "Set the ElevenLabs voice ID for AI voice TTS",
})

create_or_replace_user_command("AIVoiceElevenLabsModel", function(opts)
  set_elevenlabs_model_id(opts.args)
end, {
  nargs = "?",
  desc = "Set the ElevenLabs model ID for AI voice TTS",
})

create_or_replace_user_command("AIVoiceElevenLabsSpeed", function(opts)
  if opts.args == "" then
    vim.notify("Current ElevenLabs voice speed: " .. string.format("%.2f", elevenlabs_speed), vim.log.levels.INFO)
    return
  end

  set_elevenlabs_speed(opts.args)
end, {
  nargs = "?",
  desc = "Set the ElevenLabs voice speed for AI voice TTS",
})

create_or_replace_user_command("AIVoiceStop", function()
  ai_voice_stop()
end, {
  desc = "Stop current playback and clear queued speech",
})

create_or_replace_user_command("AIVoiceRange", function(opts)
  local lines = vim.api.nvim_buf_get_lines(0, opts.line1 - 1, opts.line2, false)
  local message = vim.trim(table.concat(lines, "\n"))

  if message == "" then
    vim.notify("AIVoiceRange requires a non-empty range", vim.log.levels.ERROR)
    return
  end

  queue_ai_voice_speak(message)
end, {
  range = true,
  desc = "Queue the selected line range for playback",
})

create_or_replace_user_command("AIVoiceReplay", function()
  replay_last_audio_message()
end, {
  desc = "Replay the last AI voice message",
})

create_or_replace_user_command("AIToggleVoice", function()
  set_ai_voice_enabled(not ai_voice_enabled)

  if not ai_voice_enabled then
    ai_voice_stop()
  end
end, {
  desc = "Toggle AI voice playback",
})

create_or_replace_user_command("AIEnableVoice", function()
  set_ai_voice_enabled(true)
end, {
  desc = "Enable AI voice playback",
})

create_or_replace_user_command("AIDisableVoice", function()
  set_ai_voice_enabled(false)
  ai_voice_stop()
end, {
  desc = "Disable AI voice playback",
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
