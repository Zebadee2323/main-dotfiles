local tools, registrations = {}, 0
package.loaded.naia = {
  register_tool = function(name, spec) tools[name] = spec; registrations = registrations + 1; return true end,
  deregister_tool = function(name) tools[name] = nil; return true end,
}
local function call(name, args)
  return vim.json.decode(assert(tools['walkthrough_' .. name]).callback(args or {}))
end
local tour = require('naia.walkthrough')
assert(not package.loaded['naia.walkthrough.runtime'], 'Facade require must not start the runtime')
assert(vim.fn.exists(':AIWalkStop') == 0)
assert(tour.stop() == false)
vim.g.ai_voice_enabled = false
vim.cmd('filetype plugin off')
tour.setup()
local registered = registrations
local autocmds = #vim.api.nvim_get_autocmds({ group = 'ai_walkthrough' })
local path = vim.fn.tempname() .. '.txt'
vim.fn.writefile({ 'first', 'second' }, path)
local steps = {
  { path = path, line_start = 1, line_end = 1, description = 'First' },
  { path = path, line_start = 2, line_end = 2, description = 'Second' },
}
local restored = false
vim.keymap.set('n', ']w', function() restored = true end)
vim.keymap.set('n', '[w', 'global-previous')
local local_buffer = vim.api.nvim_get_current_buf()
vim.keymap.set('n', '[w', 'local-previous', { buffer = local_buffer })
local started = call('start', { steps = steps, pause_after_step = true, confirm_start = false })
local context = call('current_context')
tour.setup()
require('naia.walkthrough.runtime').setup()
assert(registrations == registered, 'Repeated setup must not replace tool handlers')
assert(#vim.api.nvim_get_autocmds({ group = 'ai_walkthrough' }) == autocmds)
local same = call('current_context')
assert(same.walkthrough_id == started.walkthrough_id and same.playback_revision == context.playback_revision,
  'Repeated setup must not stop, reset or replay an active tour')
assert(_G.ai_walkthrough_stop_current == nil and _G.ai_walkthrough_foldtext == nil)

-- Stop also dismisses an outstanding confirmation, without leaving a callback
-- able to resurrect playback later.
assert(call('start', { steps = steps }).status == 'awaiting_confirmation')
assert(tour.stop())
for _, win in ipairs(vim.api.nvim_list_wins()) do
  assert(not vim.b[vim.api.nvim_win_get_buf(win)].naia_walkthrough_confirmation)
end
vim.fn.maparg(']w', 'n', false, true).callback()
assert(restored, 'Stopping must restore the previous user mapping')
assert(vim.fn.maparg('[w', 'n', false, true).rhs == 'local-previous', 'Buffer-local shadows must survive')
vim.keymap.del('n', '[w', { buffer = local_buffer })
assert(vim.fn.maparg('[w', 'n', false, true).rhs == 'global-previous', 'Restore the global map, not its buffer-local shadow')
assert(tour.stop() == false)

-- Even the no-timer fallback must invalidate a scheduled advance on stop.
local new_timer, schedule = vim.uv.new_timer, vim.schedule
local queued = {}
vim.uv.new_timer = function() return nil end
vim.schedule = function(callback) queued[#queued + 1] = callback end
call('start', { steps = steps, confirm_start = false })
assert(#queued > 0, 'Fixture must queue a fallback advance')
tour.stop()
vim.uv.new_timer, vim.schedule = new_timer, schedule
for _, callback in ipairs(queued) do callback() end
local ok = pcall(call, 'current_context')
assert(not ok, 'Stopped playback must not be resurrected by late callbacks')
vim.fn.delete(path)
print('PASS: lazy facade, singleton setup, active-tour preservation, stop and stale scheduled callbacks')
