#!/usr/bin/env python3

import json
import os
import pathlib
import runpy
import subprocess
import unittest
from unittest.mock import Mock, patch


ROOT = pathlib.Path(__file__).resolve().parents[2]
MODULE = runpy.run_path(str(ROOT / "bin" / "sidekick-naia-cli"), run_name="sidekick_naia_cli_test")
CONNECTION = {"url": "http://127.0.0.1:43123/", "token": "test-private-token"}


class ConnectionTests(unittest.TestCase):
    def test_connection_discovery_reads_current_credentials_without_starting_upstream(self):
        result = subprocess.CompletedProcess([], 0, stdout=json.dumps(CONNECTION), stderr="")
        with patch.dict("os.environ", {"NVIM": "/editor/socket"}), patch("subprocess.run", return_value=result) as run:
            self.assertEqual(MODULE["get_naia_connection_info"](), CONNECTION)
        self.assertEqual(run.call_args.kwargs["timeout"], 30)
        self.assertIn("naia.connection_info()", run.call_args.args[0][-1])
        self.assertNotIn("naia.start()", run.call_args.args[0][-1])

    def test_connection_discovery_ignores_editor_environment(self):
        payload = {**CONNECTION, "launch_env": {"EDITOR_PLACEMENT": "ignored"}}
        result = subprocess.CompletedProcess([], 0, stdout=json.dumps(payload), stderr="")
        with patch.dict("os.environ", {"NVIM": "/editor/socket"}), patch("subprocess.run", return_value=result) as run:
            self.assertEqual(MODULE["get_naia_connection_info"](), CONNECTION)
        expression = run.call_args.args[0][-1]
        self.assertNotIn("vim.env", expression)
        self.assertNotIn("launch_env", expression)

    def test_all_harnesses_inherit_caller_environment_without_editor_placement(self):
        for target in ("opencode", "codex", "claude"):
            execute = Mock()
            with self.subTest(target=target), patch.dict("os.environ", {"PATH": os.environ["PATH"], "CALLER_CONTEXT": "unchanged"}, clear=True), patch.dict(MODULE["main"].__globals__, {
                "get_naia_connection_info": Mock(return_value={**CONNECTION, "launch_env": {"EDITOR_PLACEMENT": "ignored"}}),
                "notify_nvim": Mock(),
            }), patch.object(MODULE["sys"], "argv", ["sidekick-naia-cli", target]), patch("os.execvpe", execute):
                self.assertEqual(MODULE["main"](), 0)
            env = execute.call_args.args[2]
            self.assertEqual(env["CALLER_CONTEXT"], "unchanged")
            self.assertNotIn("EDITOR_PLACEMENT", env)

    def test_bad_discovery_is_recoverable_and_does_not_log_payloads(self):
        warn = Mock()
        for raw in ("not json", "[]", "null", '{"token":"secret"}', '{"connection_file":42}',
                    json.dumps({**CONNECTION, "url": "http://evil.test/"}),
                    json.dumps({**CONNECTION, "url": "http://127.0.0.1/?secret=x"}),
                    json.dumps({**CONNECTION, "token": "header\ninjection"})):
            result = subprocess.CompletedProcess([], 0, stdout=raw, stderr="")
            with self.subTest(raw=raw), patch.dict("os.environ", {"NVIM": "/socket"}), patch("subprocess.run", return_value=result), patch.dict(MODULE["get_naia_connection_info"].__globals__, {"warn": warn}):
                self.assertIsNone(MODULE["get_naia_connection_info"]())
        self.assertNotIn("secret", str(warn.call_args_list))
        with patch.dict("os.environ", {"NVIM": "/socket"}), patch("subprocess.run", side_effect=subprocess.TimeoutExpired("nvim", 30)), patch.dict(MODULE["get_naia_connection_info"].__globals__, {"warn": warn}):
            self.assertIsNone(MODULE["get_naia_connection_info"]())

    def test_claude_connects_directly_without_replacing_other_servers(self):
        args = MODULE["build_claude_args"](["--resume"], CONNECTION)
        config = json.loads(args[args.index("--mcp-config") + 1])["mcpServers"]["naia"]
        self.assertEqual(config["type"], "http")
        self.assertEqual(config["url"], CONNECTION["url"])
        self.assertEqual(config["headers"]["Authorization"], "Bearer ${NAIA_MCP_TOKEN}")
        self.assertNotIn(CONNECTION["token"], str(args))
        self.assertNotIn("command", config)
        self.assertNotIn("--strict-mcp-config", args)
        self.assertNotIn("--allowedTools", args)
        self.assertFalse(any("approval_mode" in arg for arg in MODULE["build_codex_args"]([], CONNECTION)))
        self.assertEqual(args[-1], "--resume")

    def test_codex_uses_native_http_and_instruction_file(self):
        args = MODULE["build_codex_args"](["exec"], CONNECTION)
        inline = next(arg for arg in args if arg.startswith("mcp_servers.naia="))
        self.assertIn('url = "http://127.0.0.1:43123/"', inline)
        self.assertIn('bearer_token_env_var = "NAIA_MCP_TOKEN"', inline)
        self.assertNotIn(CONNECTION["token"], str(args))
        self.assertNotIn("command", inline)
        self.assertIn("model_instructions_file=", args[-2])
        self.assertEqual(args[-1], "exec")

    def test_claude_and_codex_main_forward_only_environment_credentials(self):
        for target in ("claude", "codex"):
            execute = Mock()
            with self.subTest(target=target), patch.dict(MODULE["main"].__globals__, {
                "get_naia_connection_info": Mock(return_value=CONNECTION), "notify_nvim": Mock(),
            }), patch.object(MODULE["sys"], "argv", ["sidekick-naia-cli", target]), patch("os.execvpe", execute):
                self.assertEqual(MODULE["main"](), 0)
            self.assertNotIn(CONNECTION["token"], str(execute.call_args.args[1]))
            self.assertEqual(execute.call_args.args[2]["NAIA_MCP_TOKEN"], CONNECTION["token"])

    def test_missing_launch_instructions_fail_closed(self):
        for target in ("claude", "codex", "opencode"):
            execute = Mock()
            with self.subTest(target=target), patch.dict(MODULE["main"].__globals__, {
                "get_naia_connection_info": Mock(return_value=CONNECTION), "notify_nvim": Mock(),
                "read_agent_instructions": Mock(return_value=""), "warn": Mock(),
            }), patch.object(MODULE["sys"], "argv", ["sidekick-naia-cli", target]), patch("os.execvpe", execute):
                self.assertEqual(MODULE["main"](), 1)
            execute.assert_not_called()

    def test_shipped_instructions_reference_native_tools(self):
        text = pathlib.Path(MODULE["AGENT_INSTRUCTIONS_FILE"]).read_text()
        self.assertIn("nvim_inform_user", text)
        self.assertIn("nvim_walkthrough_start", text)
        self.assertNotIn("ai_walkthrough_", text)


class OpenCodePrivateTests(unittest.TestCase):
    def setUp(self):
        self.globals = MODULE["build_opencode_args"].__globals__
        self.env = {"OPENCODE_CONFIG_CONTENT": "{}"}

    def build(self, argv):
        return MODULE["build_opencode_args"](argv)

    def test_launch_only_config_supplies_plugin_and_native_mcp(self):
        env = MODULE["build_opencode_env"](self.env, CONNECTION)
        config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
        naia = config["mcp"]["servers"]["naia"]
        self.assertEqual(naia["type"], "remote")
        self.assertEqual(naia["url"], CONNECTION["url"])
        self.assertEqual(naia["headers"]["Authorization"], "Bearer {env:NAIA_MCP_TOKEN}")
        self.assertFalse(naia["oauth"])
        self.assertFalse(naia["disabled"])
        self.assertNotIn("enabled", naia)
        self.assertNotIn("command", naia)
        self.assertEqual(env["NAIA_MCP_TOKEN"], CONNECTION["token"])
        self.assertNotIn(CONNECTION["token"], env["OPENCODE_CONFIG_CONTENT"])
        self.assertEqual(config["plugins"], [MODULE["OPENCODE_PLUGIN_DIR"]])
        self.assertNotIn("instructions", config)
        self.assertEqual(self.env, {"OPENCODE_CONFIG_CONTENT": "{}"})

    def test_preserves_config_and_replaces_legacy_naia(self):
        original = {
            "model": "provider/model",
            "mcp": {
                "naia": {"type": "local", "command": ["old"], "enabled": True},
                "other_legacy": {"type": "local", "command": ["legacy"]},
                "servers": {
                    "other": {"type": "local", "command": ["other"]},
                    "naia": {"type": "local", "command": ["old"], "environment": {"OLD": "1"}},
                },
                "timeout": {"catalog": 40000},
            },
            "instructions": [MODULE["AGENT_INSTRUCTIONS_FILE"], "other.md"],
            "plugins": ["existing-plugin", {"package": "another-plugin", "options": {"x": 1}}],
        }
        env = {"OPENCODE_CONFIG_CONTENT": json.dumps(original)}
        result = MODULE["build_opencode_env"](env, CONNECTION)
        config = json.loads(result["OPENCODE_CONFIG_CONTENT"])
        self.assertEqual(env["OPENCODE_CONFIG_CONTENT"], json.dumps(original))
        self.assertEqual(config["model"], original["model"])
        self.assertEqual(config["plugins"], original["plugins"] + [MODULE["OPENCODE_PLUGIN_DIR"]])
        self.assertEqual(config["instructions"], ["other.md"])
        self.assertEqual(config["mcp"]["servers"]["other"], original["mcp"]["servers"]["other"])
        self.assertEqual(config["mcp"]["other_legacy"], original["mcp"]["other_legacy"])
        self.assertEqual(config["mcp"]["timeout"], original["mcp"]["timeout"])
        self.assertNotIn("naia", config["mcp"])
        self.assertNotIn("environment", config["mcp"]["servers"]["naia"])
        again = MODULE["build_opencode_env"](result, CONNECTION)
        self.assertEqual(json.loads(again["OPENCODE_CONFIG_CONTENT"]), config)

    def test_invalid_inline_config(self):
        for raw in ("not json", "[]", "null"):
            with self.subTest(raw=raw), patch.dict(self.globals, {"warn": Mock()}):
                env = MODULE["build_opencode_env"]({"OPENCODE_CONFIG_CONTENT": raw}, CONNECTION)
                self.assertIn("naia", json.loads(env["OPENCODE_CONFIG_CONTENT"])["mcp"]["servers"])

    def test_invalid_plugins_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "plugins"):
            MODULE["build_opencode_env"]({"OPENCODE_CONFIG_CONTENT": '{"plugins":"oops"}'}, CONNECTION)

    def test_session_options_are_preserved_without_api_preparation(self):
        for argv in ([], ["mini", "-s", "ses_existing", "--replay"], ["--session=ses_new"],
                     ["run", "-c", "explain this"], ["--continue"],
                     ["run", "--session", "ses_parent", "--fork", "review"],
                     [str(ROOT), "--auto"],
                     ["run", "--agent", "plan", "--title", "Review", "--model", "provider/model", "--file", "notes.txt", "hello"]):
            with self.subTest(argv=argv), patch("subprocess.run", side_effect=AssertionError("no preparation API")):
                self.assertEqual(self.build(argv), argv + ["--standalone"])

    def test_explicit_standalone_is_not_duplicated(self):
        self.assertEqual(self.build(["--standalone"]), ["--standalone"])
        self.assertEqual(self.build(["mini", "--standalone", "-c"]), ["mini", "--standalone", "-c"])

    def test_options_are_inserted_before_literal_arguments(self):
        self.assertEqual(self.build(["run", "--", "--help"]), ["run", "--standalone", "--", "--help"])
        self.assertEqual(self.build(["--", str(ROOT)]), ["--standalone", "--", str(ROOT)])

    def test_non_session_commands_are_unchanged(self):
        for argv in (["--help"], ["--version"], ["run", "--help"], ["api", "get", "/api/info"], ["service", "status"], ["mcp", "list"], ["debug", "config"]):
            with self.subTest(argv=argv):
                self.assertEqual(self.build(argv), argv)

    def test_missing_instructions_cancel_preparation(self):
        for text in (None, "", "   "):
            with self.subTest(text=text), patch.dict(self.globals, {"read_agent_instructions": Mock(return_value=text)}):
                with self.assertRaises(ValueError):
                    self.build([])

    def test_missing_plugin_cancels_preparation(self):
        with patch.dict(self.globals, {"OPENCODE_PLUGIN_DIR": "/missing/naia-plugin"}):
            with self.assertRaisesRegex(ValueError, "plugin"):
                self.build([])

    def test_in_memory_private_server_is_supported(self):
        self.env["OPENCODE_DB"] = ":memory:"
        self.assertEqual(self.build([]), ["--standalone"])

    def test_explicit_server_is_rejected(self):
        for argv in (["--server", "https://example.test"], ["--server=https://example.test"], ["--standalone", "--server", "https://example.test"]):
            with self.subTest(argv=argv), self.assertRaisesRegex(ValueError, "explicit --server"):
                self.build(argv)

    def test_missing_option_value_is_rejected(self):
        with self.assertRaises(ValueError):
            self.build(["--session"])

    def test_main_launches_private_with_config_and_no_preparation_requests(self):
        execute = Mock()
        with patch.dict(self.globals, {"get_naia_connection_info": Mock(return_value=CONNECTION), "notify_nvim": Mock()}), patch.object(MODULE["sys"], "argv", ["sidekick-naia-cli", "opencode", "mini"]), patch("os.execvpe", execute), patch("subprocess.run", side_effect=AssertionError("no API")):
            self.assertEqual(MODULE["main"](), 0)
        self.assertEqual(execute.call_args.args[:2], ("opencode", ["opencode", "mini", "--standalone"]))
        env = execute.call_args.args[2]
        self.assertEqual(env["NAIA_MCP_TOKEN"], CONNECTION["token"])
        self.assertIn(MODULE["OPENCODE_PLUGIN_DIR"], json.loads(env["OPENCODE_CONFIG_CONTENT"])["plugins"])
        self.assertNotIn(CONNECTION["token"], str(execute.call_args.args[1]))

    def test_main_does_not_inject_naia_into_administrative_commands(self):
        execute = Mock()
        with patch.dict(self.globals, {"get_naia_connection_info": Mock(return_value=CONNECTION), "build_opencode_env": Mock(side_effect=AssertionError("must not inject"))}), patch.object(MODULE["sys"], "argv", ["sidekick-naia-cli", "opencode", "service", "status"]), patch("os.execvpe", execute):
            self.assertEqual(MODULE["main"](), 0)
        self.assertEqual(execute.call_args.args[:2], ("opencode", ["opencode", "service", "status"]))
        self.assertNotIn("NAIA_MCP_TOKEN", execute.call_args.args[2])

    def test_missing_bridge_preserves_non_naia_launch(self):
        execute = Mock()
        with patch.dict(self.globals, {"get_naia_connection_info": Mock(return_value=None), "notify_nvim": Mock(), "warn": Mock()}), patch.dict(os.environ, {"NAIA_MCP_TOKEN": "stale"}), patch.object(MODULE["sys"], "argv", ["sidekick-naia-cli", "opencode"]), patch("os.execvpe", execute):
            self.assertEqual(MODULE["main"](), 0)
        self.assertEqual(execute.call_args.args[:2], ("opencode", ["opencode"]))
        self.assertNotIn("NAIA_MCP_TOKEN", execute.call_args.args[2])


if __name__ == "__main__":
    unittest.main()
