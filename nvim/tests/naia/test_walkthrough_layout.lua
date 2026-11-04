local layout = require("naia.walkthrough.layout")
local function fill(first, last, value)
  local rows = {}
  for row = first, last do rows[row] = value end
  return rows
end

-- Put the entire float (including its border) below the changed band.
local placement = layout.choose(30, 8, fill(0, 13, 2), 2)
assert(placement.row >= 14 and placement.height == 8)
assert(placement.changes == 0)

-- Choose the top gap when the lower viewport contains changes.
placement = layout.choose(30, 8, fill(12, 29, 2), 18)
assert(placement.row + placement.height + 1 < 12)

-- A full box cannot fit: preserve the largest clear shortened version.
placement = layout.choose(20, 12, fill(0, 9, 2), 0)
assert(placement.row == 10 and placement.height == 8 and placement.changes == 0)

-- Dense diff: prioritize the explained rows over other changes.
local rows = fill(0, 19, 2)
for row = 5, 12 do rows[row] = 3 end
placement = layout.choose(20, 10, rows, 5)
assert(placement.focus == 0 and placement.height == 3)
assert(placement.row + placement.height + 2 <= 20)

-- Padding and borders count; a four-row opening cannot fit three text rows.
rows = fill(0, 19, 2)
for row = 8, 11 do rows[row] = nil end
placement = layout.choose(20, 3, rows, 8)
assert(placement.changes > 0)
assert(layout.choose(2, 5, {}, 0) == nil)
placement = layout.choose(3, 10, {}, 99)
assert(placement.row == 0 and placement.height == 1)

-- Even a failed split operation must restore the user's global preference
-- without equalizing an existing uneven layout in the process.
vim.cmd("vnew")
local side = vim.api.nvim_get_current_win()
vim.api.nvim_win_set_width(side, 23)
local width = vim.api.nvim_win_get_width(side)
local ok, err = pcall(layout.without_equalizing, function() error("split failed", 0) end)
assert(not ok and err == "split failed")
assert(vim.o.equalalways and vim.api.nvim_win_get_width(side) == width)
vim.o.equalalways = false
assert(layout.without_equalizing(function() return 42 end) == 42)
assert(not vim.o.equalalways and vim.api.nvim_win_get_width(side) == width)
print("walkthrough layout tests passed")
