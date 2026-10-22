local M = {}

function M.setup()
  -- Keep custom runtime features in the post-plugin phase, in their original order.
  require("features.voice").setup()
  require("features.file_change_fx").setup()
  require("features.paths")
  require("features.git_cheat_sheet")
  require("features.git")
  require("features.guid")
  require("core.neovide")
  require("features.unity")
end

return M
