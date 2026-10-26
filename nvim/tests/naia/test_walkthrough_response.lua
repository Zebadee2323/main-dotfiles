local response = require('naia.walkthrough.response')
local function normalize(value)
  if type(value) ~= 'table' then return nil end
  local steps = value.steps or value.walkthrough or value.items or value
  if type(steps) == 'table' and steps[1] and steps[1].path then return steps end
end

-- Exercise the dependency-free fallback even when CodeCompanion is installed.
package.preload['codecompanion.utils.yaml'] = function() error('unavailable') end
local steps = assert(response.parse([[# ai-walkthrough
Here is the tour:
steps:
  - path: "example.lua"
    line: 2
    enabled: true
    optional: null
    description: |
      First line
      Second line
]], normalize))
assert(steps[1].path == 'example.lua' and steps[1].line == 2)
assert(steps[1].enabled == true and steps[1].optional == nil)
assert(steps[1].description:find('First line', 1, true))
assert(steps[1].description:find('Second line', 1, true))
local json = assert(response.parse('{"steps":[{"path":"json.lua","line":4}]}', normalize))
assert(json[1].path == 'json.lua' and json[1].line == 4)
assert(response.json('# ai-walkthrough\n{"version":3,"steps":[]}').version == 3)
assert(response.json('not json') == nil)
local missing, err = response.parse('Not a tour', normalize)
assert(not missing and err:find('tree-sitter is unavailable', 1, true))

-- Prefer the optional YAML decoder; malformed output still falls back to JSON.
package.loaded['codecompanion.utils.yaml'] = {
  decode = function() return { { path = 'decoder.lua' } } end,
}
assert(response.parse('custom YAML', normalize)[1].path == 'decoder.lua')
package.loaded['codecompanion.utils.yaml'].decode = function() error('invalid YAML') end
assert(response.parse('[{"path":"fallback.lua"}]', normalize)[1].path == 'fallback.lua')
local _, yaml_err = response.parse('', normalize)
assert(yaml_err == 'No valid walkthrough YAML was found')
assert(not package.loaded['naia.walkthrough'], 'Response decoding must not initialize playback')
print('PASS: standalone legacy response parsing, YAML/JSON fallback and multiline scalars')
