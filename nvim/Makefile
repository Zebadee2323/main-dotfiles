NVIM_BIN ?= nvim
NODE_BIN ?= node
PYTHON_BIN ?= python3

.PHONY: check test test-config test-naia test-integration test-opencode-integration

check: test

test: test-config test-naia

# Each spec gets its own editor so mocks and startup state cannot leak.
test-config:
	@set -e; for spec in tests/*_spec.lua; do \
		$(NVIM_BIN) --headless -u NONE -l "$$spec"; \
	done

test-naia:
	@set -e; for spec in tests/naia/test_*.lua; do \
		$(NVIM_BIN) --headless -u tests/naia/minimal_init.lua -l "$$spec"; \
	done
	$(PYTHON_BIN) tests/naia/test_sidekick_naia_cli.py
	$(NODE_BIN) --experimental-vm-modules --test tests/naia/test_opencode_plugin.mjs

# Opt-in: starts isolated editors with the installed upstream bridge.
test-integration:
	MCP_TOOLS_PATH="$${MCP_TOOLS_PATH:-$$HOME/.local/share/nvim/lazy/mcp-tools.nvim}" \
	MCP_TOOLS_RUNTIME="$(CURDIR)/lua/features/mcp_tools_runtime.lua" \
	NVIM_BIN=$(NVIM_BIN) $(PYTHON_BIN) tests/naia/test_integration.py

# Opt-in: installed OpenCode, isolated database, local fake model (no real model).
test-opencode-integration:
	NAIA_TEST_OPENCODE=1 $(PYTHON_BIN) tests/naia/test_opencode_private_integration.py
