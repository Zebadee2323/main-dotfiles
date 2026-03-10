vim.opt.termguicolors = true
vim.opt.background = "dark"

vim.cmd.colorscheme("ashen")

if vim.g.neovide then
    vim.api.nvim_set_hl(0, "Normal", { bg = "#272727" })
    vim.api.nvim_set_hl(0, "NormalFloat", { bg = "#272727" })
else
    vim.api.nvim_set_hl(0, "Normal", { bg = "none" })
    vim.api.nvim_set_hl(0, "NormalFloat", { bg = "none" })
end
vim.api.nvim_set_hl(0, "Cursor", { fg = "#000000", bg = "#ff7d17" })
vim.api.nvim_set_hl(0, "TermCursor", { fg = "#000000", bg = "#ff7d17" })
vim.api.nvim_set_hl(0, "diffAdded", { fg = "#00bb00" })
vim.api.nvim_set_hl(0, "diffSubname", { fg = "#c4693d" })

local function hex_to_rgb(hex)
    hex = hex:gsub("#", "")
    return {
        r = tonumber(hex:sub(1, 2), 16),
        g = tonumber(hex:sub(3, 4), 16),
        b = tonumber(hex:sub(5, 6), 16),
    }
end

local function rgb_to_hex(rgb)
    local clamp = function(v)
        return math.max(0, math.min(255, math.floor(v)))
    end
    return string.format("#%02x%02x%02x", clamp(rgb.r), clamp(rgb.g), clamp(rgb.b))
end

-- Convert RGB to HSL
local function rgb_to_hsl(rgb)
    local r, g, b = rgb.r / 255, rgb.g / 255, rgb.b / 255
    local maxc = math.max(r, g, b)
    local minc = math.min(r, g, b)
    local h, s
    local l = (maxc + minc) / 2

    if maxc == minc then
        h, s = 0, 0
    else
        local d = maxc - minc
        s = l > 0.5 and d / (2 - maxc - minc) or d / (maxc + minc)

        if maxc == r then
            h = (g - b) / d + (g < b and 6 or 0)
        elseif maxc == g then
            h = (b - r) / d + 2
        else
            h = (r - g) / d + 4
        end
        h = h / 6
    end

    return { h = h, s = s, l = l }
end

-- Convert HSL back to RGB
local function hsl_to_rgb(hsl)
    local h, s, l = hsl.h, hsl.s, hsl.l

    if s == 0 then
        local v = l * 255
        return { r = v, g = v, b = v }
    end

    local function hue_to_rgb(p, q, t)
        if t < 0 then t = t + 1 end
        if t > 1 then t = t - 1 end
        if t < 1 / 6 then return p + (q - p) * 6 * t end
        if t < 1 / 2 then return q end
        if t < 2 / 3 then return p + (q - p) * (2 / 3 - t) * 6 end
        return p
    end

    local q = l < 0.5 and l * (1 + s) or (l + s - l * s)
    local p = 2 * l - q

    return {
        r = 255 * hue_to_rgb(p, q, h + 1 / 3),
        g = 255 * hue_to_rgb(p, q, h),
        b = 255 * hue_to_rgb(p, q, h - 1 / 3),
    }
end

-- ---- COMMAND ----

vim.api.nvim_create_user_command("LightenBackground", function(opts)
    local percent = tonumber(opts.args)
    if not percent then
        print("Usage: :LightenBackground 10")
        return
    end

    local normal = vim.api.nvim_get_hl(0, { name = "Normal" })
    if not normal.bg then
        print("Normal has no bg set")
        return
    end

    -- Convert integer (e.g. 0x1a1b26) → "#1a1b26"
    local bg_hex = string.format("#%06x", normal.bg)

    local rgb = hex_to_rgb(bg_hex)
    local hsl = rgb_to_hsl(rgb)

    -- Adjust lightness by percentage
    hsl.l = math.min(1, math.max(0, hsl.l + hsl.l * (percent / 100)))

    local new_rgb = hsl_to_rgb(hsl)
    local new_hex = rgb_to_hex(new_rgb)

    vim.api.nvim_set_hl(0, "Normal", { bg = new_hex })
    vim.api.nvim_set_hl(0, "NormalFloat", { bg = new_hex })

    print("Background changed → " .. new_hex)
end, { nargs = 1 })
