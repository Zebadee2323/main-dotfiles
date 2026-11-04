-- Stable step IDs are independent of the current playback index.
local M = {}

function M.find(steps, id)
  for index, step in ipairs(steps or {}) do if step.id == id then return index end end
end

function M.descendants(steps, id)
  local result, changed = { [id] = false }, true
  while changed do
    changed = false
    for _, step in ipairs(steps) do
      if step.id ~= id and step.parent_id ~= nil and result[step.parent_id] ~= nil and result[step.id] == nil then
        result[step.id], changed = true, true
      end
    end
  end
  return result
end

function M.depth(steps, index)
  local depth, seen = 0, {}
  while index and steps[index] and steps[index].parent_id do
    index = M.find(steps, steps[index].parent_id)
    if not index or seen[index] then break end
    seen[index], depth = true, depth + 1
  end
  return depth
end

return M
