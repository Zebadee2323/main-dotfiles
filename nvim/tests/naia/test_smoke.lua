local helpers = dofile("tests/naia/helpers.lua")

local ok, naia = pcall(require, "naia")

helpers.truthy(ok, "require('naia') should succeed")
helpers.truthy(type(naia) == "table", "naia module should return a table")
helpers.truthy(type(naia.setup) == "function", "naia.setup should exist")
helpers.truthy(type(naia.start) == "function", "naia.start should exist")
helpers.truthy(type(naia.stop) == "function", "naia.stop should exist")
helpers.truthy(type(naia.register_tool) == "function", "naia.register_tool should exist")
helpers.truthy(type(naia.deregister_tool) == "function", "naia.deregister_tool should exist")
