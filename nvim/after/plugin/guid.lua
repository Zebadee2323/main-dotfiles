local function generate_guid()
  local uuid = vim.fn.system('uuidgen')
  if vim.v.shell_error ~= 0 then
    error('Failed to generate GUID via uuidgen')
  end

  return uuid:gsub('%s+', ''):gsub('-', ''):lower()
end

local function create_guid()
  local ok, guid = pcall(generate_guid)
  if not ok then
    vim.notify(guid, vim.log.levels.ERROR)
    return nil
  end

  return guid
end

local function insert_text_at_cursor(text)
  local row, col = unpack(vim.api.nvim_win_get_cursor(0))
  local line = vim.api.nvim_get_current_line()
  local before = line:sub(1, col)
  local after = line:sub(col + 1)

  vim.api.nvim_set_current_line(before .. text .. after)
  vim.api.nvim_win_set_cursor(0, { row, col + #text })
end

vim.api.nvim_create_user_command('CreateGuid', function()
  local guid = create_guid()
  if not guid then
    return
  end

  insert_text_at_cursor(guid)
end, {
  desc = 'Insert a generated 128-bit GUID at the cursor',
})

vim.keymap.set('n', '<Plug>(CreateGuid)', function()
  local guid = create_guid()
  if not guid then
    return
  end

  insert_text_at_cursor(guid)
end, {
  desc = 'Insert a generated 128-bit GUID at the cursor',
})

vim.keymap.set('i', '<Plug>(CreateGuid)', function()
  return create_guid() or ''
end, {
  desc = 'Insert a generated 128-bit GUID at the cursor',
  expr = true,
})
