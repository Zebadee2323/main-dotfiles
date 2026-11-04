local M = {}
local page_height = 3

-- Roughly 180 words/minute at five non-whitespace characters per word.
-- Like caption page weights, count characters rather than UTF-8 bytes.
-- This estimates speech only; synthesis and player startup are not included.
function M.estimate_talk_seconds(message)
  local weight = 0
  for word in message:gmatch("%S+") do
    weight = weight + vim.fn.strchars(word, 1)
  end
  return weight / 15
end

function M.prepare(message, width)
  local lines = {}
  local line = ""
  local function flush()
    if line ~= "" then
      lines[#lines + 1] = line
      line = ""
    end
  end

  for word in message:gmatch("%S+") do
    local candidate = line == "" and word or line .. " " .. word
    if vim.fn.strdisplaywidth(candidate) <= width then
      line = candidate
    else
      flush()
      -- Split oversized words/paths by characters, never UTF-8 bytes. Keep
      -- combining marks attached and measure terminal cells, not string length.
      for index = 0, vim.fn.strchars(word, 1) - 1 do
        local char = vim.fn.strcharpart(word, index, 1, 1)
        if vim.fn.strdisplaywidth(line .. char) > width then
          flush()
        end
        line = line .. char
      end
    end
  end
  flush()

  local pages, weight = {}, 0
  for index, text in ipairs(lines) do
    local page_index = math.floor((index - 1) / page_height) + 1
    local page = pages[page_index]
    if not page then
      page = { lines = {} }
      pages[page_index] = page
    end
    page.lines[#page.lines + 1] = text
    -- Character-weighted pages avoid giving a short final page as much time as
    -- a full one. These are estimated captions, not word-alignment timestamps.
    weight = weight + vim.fn.strchars(text, 1)
    page.finish = weight
  end
  return { pages = pages, weight = weight }
end

function M.lines(captions, elapsed, duration)
  local progress = math.max(0, math.min(1, elapsed / math.max(duration, 0.01)))
  local target = progress * captions.weight
  local page = captions.pages[#captions.pages]
  for _, candidate in ipairs(captions.pages) do
    if target < candidate.finish then
      page = candidate
      break
    end
  end
  local lines = {}
  for row = 1, page_height do
    lines[row] = page and page.lines[row] or ""
  end
  return lines
end

return M
