local voice_name = "cori-high"
local voices_dir = vim.fs.joinpath(vim.fn.stdpath("config"), "after", "ai_voices")
local ai_voice_audio_dir = vim.fn.stdpath("cache") .. "/ai-voice"
local raw_wav_path = ai_voice_audio_dir .. "/latest.wav"
local processed_wav_path = ai_voice_audio_dir .. "/latest-robot.wav"
local piper_length_scale = 1.0
local piper_noise_scale = 0.2
local piper_noise_w = 0.2
local piper_sentence_silence = 0.5
local ai_voice_volume = tonumber(vim.g.ai_voice_volume) or 1.0
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

local function start_audio_playback(wav_path, request_id)
  local play_job = vim.fn.jobstart({ "afplay", "-v", tostring(ai_voice_volume), wav_path }, {
    on_exit = function()
      set_voice_playback_active(false, { request_id = request_id })

      if speech_request_id == request_id then
        current_play_job = nil
        maybe_start_queued_speech()
      end
    end,
  })

  if play_job <= 0 then
    vim.notify("Generated audio at " .. wav_path .. " but failed to start afplay", vim.log.levels.WARN)
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
