local M = {}
local flashes = {}

local function blend(a, b, amount)
  local result = 0
  for _, shift in ipairs({ 16, 8, 0 }) do
    local first = math.floor(a / 2 ^ shift) % 256
    local second = math.floor(b / 2 ^ shift) % 256
    result = result + math.floor(first * (1 - amount) + second * amount + 0.5) * 2 ^ shift
  end
  return result
end

local function brightness(colour)
  return math.floor(colour / 65536) * 0.2126
    + (math.floor(colour / 256) % 256) * 0.7152 + (colour % 256) * 0.0722
end

function M.highlights()
  local function hl(name)
    return vim.api.nvim_get_hl(0, { name = name, link = false, create = false })
  end
  local normal, float = hl("Normal"), hl("NormalFloat")
  local light = vim.o.background == "light"
  local base = normal.bg or (light and 0xfafafa or 0x202020)
  local foreground = float.fg or normal.fg or (light and 0x202020 or 0xe0e0e0)
  local surface = float.bg
  if not surface or math.abs(brightness(surface) - brightness(base)) < 20 then
    surface = blend(base, light and 0x000000 or 0xffffff, light and 0.08 or 0.12)
  end
  -- Prefer an actual, darker theme surface for the voice panel rather than
  -- the raised background used for cards/approvals (also works with transparent Normal).
  local voice_background
  for _, name in ipairs({ "Pmenu", "NormalFloat", "StatusLine" }) do
    local candidate = hl(name).bg
    if candidate and candidate ~= base and brightness(candidate) < brightness(surface) - 4
        and ((light and brightness(candidate) >= 128) or (not light and brightness(candidate) < 128)) then
      voice_background = candidate
      break
    end
  end
  voice_background = voice_background or blend(light and surface or base, 0x000000, 0.12)
  local roles = {
    Card = hl("DiagnosticInfo").fg or hl("Function").fg or foreground,
    Voice = hl("DiagnosticInfo").fg or hl("Function").fg or foreground,
    Approval = hl("DiagnosticWarn").fg or hl("Question").fg or foreground,
  }
  for name, accent in pairs(roles) do
    local background = name ~= "Approval" and voice_background or blend(surface, accent, 0.035)
    vim.api.nvim_set_hl(0, "NaiaUI" .. name .. "Normal", { fg = foreground, bg = background })
    vim.api.nvim_set_hl(0, "NaiaUI" .. name .. "Border", { fg = accent, bg = background })
    vim.api.nvim_set_hl(0, "NaiaUI" .. name .. "Title", { fg = accent, bg = background, bold = true })
    vim.api.nvim_set_hl(0, "NaiaUI" .. name .. "Muted", { fg = hl("Comment").fg or foreground, bg = background })
  end
  local flash_background = blend(voice_background, foreground, 0.24)
  vim.api.nvim_set_hl(0, "NaiaUICardFlashNormal", { fg = foreground, bg = flash_background })
  vim.api.nvim_set_hl(0, "NaiaUICardFlashBorder", { fg = foreground, bg = flash_background, bold = true })
  vim.api.nvim_set_hl(0, "NaiaUICardFlashTitle", { fg = roles.Card, bg = flash_background, bold = true })
end

function M.apply(window, kind)
  if not vim.api.nvim_win_is_valid(window) then return end
  local prefix = ({ approval = "NaiaUIApproval", card = "NaiaUICard", voice = "NaiaUIVoice" })[kind]
  assert(prefix, "Unknown Naia surface kind")
  if kind == "card" and vim.w[window].naia_card_flashing then prefix = "NaiaUICardFlash" end
  vim.wo[window].winhighlight = table.concat({
    "Normal:" .. prefix .. "Normal", "NormalNC:" .. prefix .. "Normal",
    "FloatBorder:" .. prefix .. "Border", "FloatTitle:" .. prefix .. "Title",
  }, ",")
  vim.wo[window].winblend = 0
end

function M.flash(window)
  if vim.g.naia_ui_card_flash == false or not vim.api.nvim_win_is_valid(window)
      or flashes[window] then return end
  local token = {}
  flashes[window] = token
  local function clear()
    if flashes[window] == token then
      flashes[window] = nil
      if vim.api.nvim_win_is_valid(window) then
        vim.w[window].naia_card_flashing = nil
        M.apply(window, "card")
      end
    end
  end
  local function valid()
    return flashes[window] == token and vim.api.nvim_win_is_valid(window) and vim.g.naia_ui_card_flash ~= false
  end
  -- Paint normally first, then three short, soft pulses. Every step checks its
  -- window/token so closing, replacing, or muting the card cancels the sequence.
  local function pulse(number)
    if not valid() then clear(); return end
    vim.w[window].naia_card_flashing = true
    M.apply(window, "card")
    vim.defer_fn(function()
      if not valid() then clear(); return end
      vim.w[window].naia_card_flashing = nil
      M.apply(window, "card")
      if number < 3 then
        vim.defer_fn(function() pulse(number + 1) end, 100)
      else
        flashes[window] = nil
      end
    end, 150)
  end
  vim.defer_fn(function() pulse(1) end, 100)
end

-- Recognise already-open panels too, so styles can be reloaded without losing
-- approval callbacks, companion buffers, card history, or session policies.
function M.refresh()
  for _, window in ipairs(vim.api.nvim_list_wins()) do
    local config = vim.api.nvim_win_get_config(window)
    local title = config.title
    local label = type(title) == "table" and title[1] and title[1][1] or title
    if config.relative ~= "" and type(label) == "string" then
      if label == " N.A.I.A · Approval " then
        M.apply(window, "approval")
      elseif label == " N.A.I.A " then
        M.apply(window, "voice")
      elseif label:sub(1, #" N.A.I.A · ") == " N.A.I.A · " and config.zindex == 40 then
        M.apply(window, "card")
      end
    end
  end
end

M.highlights()
local group = vim.api.nvim_create_augroup("NaiaUISurfaces", { clear = true })
vim.api.nvim_create_autocmd("ColorScheme", { group = group, callback = M.highlights })
vim.api.nvim_create_autocmd("WinNew", { group = group, callback = function() vim.schedule(M.refresh) end })

return M
