-- Treesitter (nvim-treesitter `main` branch) -------------------------------------------------------------------------
-- The `main` branch dropped the old module system (`nvim-treesitter.configs`),
-- so the previous `configs.setup{...}` config silently did nothing. On `main`:
--   * parsers + queries install into stdpath('data')/site (already on rtp),
--   * highlighting is Neovim core: `vim.treesitter.start()`,
--   * folding is Neovim core: foldexpr = v:lua.vim.treesitter.foldexpr().
-- We wire both up per-buffer in a FileType autocmd below.
local ok, ts = pcall(require, "nvim-treesitter")
if not ok then
  return
end

ts.setup({})

-- Parsers to keep installed. install() is async and skips parsers that are
-- already present, so this is cheap after the first run. Add languages here
-- (or just run :TSInstall <lang>) to get highlighting + folds for them.
local ensure = {
  "c_sharp", "lua", "vim", "vimdoc", "query",
  "markdown", "markdown_inline", "bash", "json", "yaml",
}
do
  local have = {}
  for _, lang in ipairs(ts.get_installed("parsers")) do
    have[lang] = true
  end
  local missing = {}
  for _, lang in ipairs(ensure) do
    if not have[lang] then
      table.insert(missing, lang)
    end
  end
  if #missing > 0 then
    ts.install(missing)
  end
end

local MAX_FILESIZE = 200 * 1024 -- skip TS on files larger than 200 KB (matches old config)

-- Filetypes whose folding is configured elsewhere (see folding.lua); don't let
-- Treesitter override their foldmethod.
local fold_managed_elsewhere = { git = true }

local group = vim.api.nvim_create_augroup("user_treesitter", { clear = true })
vim.api.nvim_create_autocmd("FileType", {
  group = group,
  callback = function(ev)
    local lang = vim.treesitter.language.get_lang(ev.match)
    if not lang then
      return
    end

    -- Only continue if a parser for this language is actually installed.
    -- (pcall guards the call; `added` is language.add's real boolean result.)
    local added_ok, added = pcall(vim.treesitter.language.add, lang)
    if not added_ok or not added then
      return
    end

    -- Skip huge files: parsing + highlight can be slow.
    local name = vim.api.nvim_buf_get_name(ev.buf)
    local stat_ok, stat = pcall(vim.loop.fs_stat, name)
    if stat_ok and stat and stat.size > MAX_FILESIZE then
      return
    end

    -- Highlighting (provided by Neovim core).
    pcall(vim.treesitter.start, ev.buf, lang)

    -- Treesitter folding, window-local to this buffer+window. Global foldlevel
    -- is 99 (editor.lua) so nothing starts folded; use zM/za to collapse.
    if not fold_managed_elsewhere[ev.match] then
      vim.wo[0][0].foldmethod = "expr"
      vim.wo[0][0].foldexpr = "v:lua.vim.treesitter.foldexpr()"
    end
  end,
})
