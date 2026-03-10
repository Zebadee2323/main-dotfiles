local ok, configs = pcall(require, "nvim-treesitter.configs")
if not ok then
  return
end

configs.setup({
  -- Pick what you actually use. "all" is fine too, but slower to install/update.
  ensure_installed = { "c_sharp", "lua", "vim", "vimdoc", "query" },

  -- Usually keep this false; true can freeze nvim while installing parsers.
  sync_install = false,

  -- Automatically install missing parsers when entering a buffer.
  auto_install = true,

  highlight = {
    enable = true,

    -- If you see duplicate highlights, keep this false.
    additional_vim_regex_highlighting = false,

    -- Optional: disable highlighting for huge files
    disable = function(_, buf)
      local max_filesize = 200 * 1024 -- 200 KB
      local ok_stat, stat = pcall(vim.loop.fs_stat, vim.api.nvim_buf_get_name(buf))
      if ok_stat and stat and stat.size > max_filesize then
        return true
      end
      return false
    end,
  },

  indent = {
    enable = true,
  },
})
