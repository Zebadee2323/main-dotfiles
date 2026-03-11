local voice_name = "en_US-hfc_female-medium"
local voices_dir = vim.fn.expand("~/.config/dotfiles/nvim/after/ai_voices")
local current_play_job = nil
local current_wav_path = nil

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

local function ai_voice_stop()
  if current_play_job and vim.fn.jobwait({ current_play_job }, 0)[1] == -1 then
    vim.fn.jobstop(current_play_job)
  end
  current_play_job = nil
  cleanup_current_wav()
end

local function ai_voice_speak(message)
  message = vim.trim(message or "")
  if message == "" then
    vim.notify("AIVoice requires a message", vim.log.levels.ERROR)
    return
  end

  ai_voice_stop()

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
    "--",
    message,
  }

  local stderr = {}
  local job_id = vim.fn.jobstart(cmd, {
    stderr_buffered = true,
    on_stderr = function(_, data)
      collect_lines(stderr, data)
    end,
    on_exit = function(_, code)
      vim.schedule(function()
        if code ~= 0 then
          local msg = #stderr > 0 and table.concat(stderr, "\n") or ("piper exited with code " .. code)
          vim.notify(msg, vim.log.levels.ERROR)
          return
        end

        local play_job = vim.fn.jobstart({ "afplay", wav_path }, {
          on_exit = function()
            if current_play_job == play_job then
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
  end
end

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
