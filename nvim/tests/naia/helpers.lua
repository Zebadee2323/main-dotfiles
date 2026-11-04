local M = {}

function M.eq(actual, expected, message)
  if actual ~= expected then
    error(message or string.format("expected %s, got %s", vim.inspect(expected), vim.inspect(actual)))
  end
end

function M.truthy(value, message)
  if not value then
    error(message or "expected value to be truthy")
  end
end

return M
