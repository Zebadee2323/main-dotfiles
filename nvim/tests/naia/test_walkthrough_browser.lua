local tree = require('naia.walkthrough.tree')
local steps = {
  { id = 1, path = 'one.lua', description = 'Root', line_start = 1 },
  { id = 2, parent_id = 1, path = 'one.lua', description = 'Child', line_start = 2 },
  { id = 3, parent_id = 1, path = 'one.lua', description = 'Sibling', line_start = 3 },
  { id = 4, parent_id = 3, path = 'one.lua', description = 'Grandchild', line_start = 4 },
  { id = 5, path = 'two.lua', description = 'Other root', line_start = 1 },
}
assert(tree.find(steps, 4) == 4 and tree.depth(steps, 4) == 2)
assert(tree.descendants(steps, 1)[1] == false and tree.descendants(steps, 1)[4] == true)
assert(tree.descendants({ steps[4], steps[3], steps[1] }, 1)[4], 'Descendants must not depend on array ordering')
assert(tree.depth({ { id = 1, parent_id = 2 }, { id = 2, parent_id = 1 } }, 1) <= 2)
local state = { active = true, steps = vim.deepcopy(steps), index = 1, walkthrough_id = 'tour', playback_revision = 1 }
local jumped, removed, interrupted, stopped
local browser = require('naia.walkthrough.browser').new(state, {
  jump = function(index) jumped = index end,
  interrupt = function() interrupted = true end,
  stop = function(voice) stopped = voice end,
  remove = function(ids)
    removed = ids
    local remaining, count = {}, 0
    for _, step in ipairs(state.steps) do
      if ids[step.id] then count = count + 1 else remaining[#remaining + 1] = step end
    end
    state.steps = remaining
    return remaining, #remaining > 0 and 1 or nil, count
  end,
})
local function key(lhs)
  for _, map in ipairs(vim.api.nvim_buf_get_keymap(state.browser_buffer, 'n')) do
    if map.lhs == lhs then map.callback(); return end
  end
  error('Missing browser mapping: ' .. lhs)
end
browser.open()
local buf, win = state.browser_buffer, state.browser_window
browser.open()
assert(state.browser_buffer == buf and state.browser_window == win, 'Opening twice must reuse the float')
assert(not vim.bo[buf].modifiable)
vim.api.nvim_win_set_cursor(win, { 6, 0 }) -- id 3, including its grandchild
key('d')
assert(interrupted and removed[3] and removed[4] and not removed[1])
assert(#state.steps == 3 and jumped == 1)
vim.api.nvim_win_set_cursor(win, { 5, 0 }) -- remaining child id 2
key('D')
assert(removed[2] and not removed[1] and #state.steps == 2)
vim.api.nvim_win_set_cursor(win, { 5, 0 }) -- root id 5
key('<CR>')
assert(jumped == 2 and not vim.api.nvim_win_is_valid(win))
browser.open()
key('q')
assert(not state.browser_window and stopped == nil)

-- A queued close/stop must not stop a tour started before the callback runs.
browser.open()
key('Q')
state.walkthrough_id = 'replacement'
vim.wait(10, function() return false end)
assert(stopped == nil)
browser.open()
key('Q')
vim.wait(10, function() return stopped ~= nil end)
assert(stopped == true)
browser.close()
print('PASS: browser reuse, navigation, branch/sibling deletion, private mapping and stale stop protection')
