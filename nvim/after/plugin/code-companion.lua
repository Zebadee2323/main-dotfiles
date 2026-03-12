local codecompanion = require("codecompanion")

codecompanion.setup({
  interactions = {
    background = {
      adapter = "opencode",
    },
    chat = {
      adapter = "opencode",
    },
    inline = {
      adapter = "opencode",
    },
    cmd = {
      adapter = "opencode",
    },
  },
  display = {
    chat = {
      fold_reasoning = false,
      show_reasoning = true,
    },
  },
  opts = {
    log_level = "DEBUG",
  },
})
