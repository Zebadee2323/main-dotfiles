#!/usr/bin/env python3
"""Isolated launcher tests; never use the real Codex home or a model."""
import json
import os
from pathlib import Path
import runpy
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
MODULE = runpy.run_path(str(ROOT / "bin/naia-attach"))
CONNECTION = {"url": "http://127.0.0.1:43123/", "token": "test-private-token"}


class AttachmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="naia-attach-test-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "codex home"
        self.env = {"PATH": os.environ["PATH"], "CODEX_HOME": str(self.home)}
        self.globals = MODULE["main"].__globals__

    def launch(self, target, argv=(), connection=CONNECTION):
        execute = Mock()
        with patch.dict(os.environ, self.env, clear=True), patch.dict(self.globals, {
            "get_naia_connection_info": Mock(return_value=connection), "warn": Mock(),
        }), patch.object(MODULE["sys"], "argv", ["naia-attach", target, *argv]), patch("os.execvpe", execute):
            result = MODULE["main"]()
        return result, execute

    def test_connection_discovery_reads_current_credentials_without_starting_bridge(self):
        result = subprocess.CompletedProcess([], 0, stdout=json.dumps(CONNECTION), stderr="")
        with patch.dict(os.environ, {"NVIM": "/editor/socket"}), patch("subprocess.run", return_value=result) as run:
            self.assertEqual(MODULE["get_naia_connection_info"](), CONNECTION)
        self.assertIn("naia.connection_info()", run.call_args.args[0][-1])
        self.assertNotIn("naia.start()", run.call_args.args[0][-1])

    def test_bad_discovery_never_logs_credentials(self):
        warn = Mock()
        for raw in ("not json", "[]", "null", '{"token":"secret"}',
                    json.dumps({**CONNECTION, "url": "http://evil.test/"}),
                    json.dumps({**CONNECTION, "token": "header\ninjection"})):
            result = subprocess.CompletedProcess([], 0, stdout=raw, stderr="")
            with self.subTest(raw=raw), patch.dict(os.environ, {"NVIM": "/socket"}), patch("subprocess.run", return_value=result), patch.dict(self.globals, {"warn": warn}):
                self.assertIsNone(MODULE["get_naia_connection_info"]())
        self.assertNotIn("secret", str(warn.call_args_list))

    def test_codex_creates_link_before_launch_and_passes_only_short_notice(self):
        code, execute = self.launch("codex", ["--no-daemon", "resume", "--last"])
        self.assertEqual(code, 0)
        link = self.home / "skills/naia"
        self.assertTrue(link.is_symlink())
        self.assertEqual(link.resolve(), MODULE["NAIA_SKILL"].resolve())
        args = execute.call_args.args[1]
        self.assertEqual(args[-3:], ["--no-daemon", "resume", "--last"])
        self.assertIn("developer_instructions=" + json.dumps(MODULE["NOTICE_FILE"].read_text().strip()), args)
        self.assertFalse(any("model_instructions_file" in arg for arg in args))
        self.assertTrue(any(arg.startswith("mcp_servers.naia=") for arg in args))
        self.assertNotIn(CONNECTION["token"], str(args))
        self.assertEqual(execute.call_args.args[2]["NAIA_MCP_TOKEN"], CONNECTION["token"])

    def test_codex_existing_correct_link_is_unchanged(self):
        MODULE["ensure_codex_skill"](self.env)
        link = self.home / "skills/naia"
        original = link.lstat().st_ino
        MODULE["ensure_codex_skill"](self.env)
        self.assertEqual(link.lstat().st_ino, original)

    def test_codex_default_home_is_used_when_codex_home_is_unset(self):
        with patch.object(Path, "home", return_value=Path(self.temp.name)):
            MODULE["ensure_codex_skill"]({})
        self.assertEqual((Path(self.temp.name) / ".codex/skills/naia").resolve(), MODULE["NAIA_SKILL"])

    def test_codex_conflicts_are_preserved_and_cancel_launch(self):
        link = self.home / "skills/naia"
        link.parent.mkdir(parents=True)
        for kind in ("file", "directory", "wrong_link", "dangling_link"):
            with self.subTest(kind=kind):
                if kind == "file":
                    link.write_text("user work")
                elif kind == "directory":
                    link.mkdir()
                    (link / "SKILL.md").write_text("user work")
                else:
                    link.symlink_to(ROOT if kind == "wrong_link" else Path(self.temp.name) / "missing")
                original = link.lstat().st_ino
                code, execute = self.launch("codex")
                self.assertEqual(code, 1)
                execute.assert_not_called()
                self.assertEqual(link.lstat().st_ino, original)
                if kind == "directory":
                    (link / "SKILL.md").unlink()
                    link.rmdir()
                else:
                    link.unlink()

    def test_codex_relative_symlink_to_same_skill_is_accepted(self):
        link = self.home / "skills/naia"
        link.parent.mkdir(parents=True)
        link.symlink_to(os.path.relpath(MODULE["NAIA_SKILL"].resolve(), link.parent.resolve()))
        MODULE["ensure_codex_skill"](self.env)

    def test_claude_uses_session_plugin_and_short_notice_without_reminder_hook(self):
        code, execute = self.launch("claude", ["--resume"])
        self.assertEqual(code, 0)
        args = execute.call_args.args[1]
        self.assertEqual(args[args.index("--plugin-dir") + 1], str(MODULE["AGENT_DIR"]))
        self.assertEqual(args[args.index("--append-system-prompt") + 1], MODULE["NOTICE_FILE"].read_text().strip())
        server = json.loads(args[args.index("--mcp-config") + 1])["mcpServers"]["naia"]
        self.assertEqual(server["headers"]["Authorization"], "Bearer ${NAIA_MCP_TOKEN}")
        self.assertNotIn("--settings", args)
        self.assertNotIn("--strict-mcp-config", args)
        self.assertNotIn("--allowedTools", args)
        self.assertFalse(self.home.exists())

    def test_opencode_uses_native_skill_and_notice_config_without_custom_plugin(self):
        code, execute = self.launch("opencode", ["mini"])
        self.assertEqual(code, 0)
        self.assertEqual(execute.call_args.args[1], ["opencode", "mini", "--standalone"])
        env = execute.call_args.args[2]
        config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
        self.assertEqual(config["skills"], [str(MODULE["SKILLS_DIR"])])
        self.assertEqual(config["instructions"], [str(MODULE["NOTICE_FILE"])])
        self.assertNotIn("plugins", config)
        self.assertFalse(config["mcp"]["servers"]["naia"]["oauth"])
        self.assertNotIn(CONNECTION["token"], env["OPENCODE_CONFIG_CONTENT"])
        self.assertFalse(self.home.exists())

    def test_opencode_preserves_other_config_and_preparation_is_idempotent(self):
        original = {"model": "provider/model", "plugins": ["user-plugin"],
                    "skills": ["user-skills"], "instructions": ["user.md"],
                    "mcp": {"timeout": {"catalog": 40000}, "servers": {
                        "other": {"type": "local", "command": ["other"]},
                        "naia": {"type": "local", "command": ["old"]}}}}
        env = {"OPENCODE_CONFIG_CONTENT": json.dumps(original)}
        result = MODULE["build_opencode_env"](env, CONNECTION)
        config = json.loads(result["OPENCODE_CONFIG_CONTENT"])
        self.assertEqual(config["plugins"], original["plugins"])
        self.assertEqual(config["skills"][0], "user-skills")
        self.assertEqual(config["instructions"][0], "user.md")
        self.assertEqual(config["mcp"]["servers"]["other"], original["mcp"]["servers"]["other"])
        self.assertNotIn("command", config["mcp"]["servers"]["naia"])
        self.assertEqual(env["OPENCODE_CONFIG_CONTENT"], json.dumps(original))
        self.assertEqual(MODULE["build_opencode_env"](result, CONNECTION), result)

    def test_missing_bridge_does_not_create_skill_or_forward_stale_credentials(self):
        self.env["NAIA_MCP_TOKEN"] = "stale"
        for target in ("opencode", "codex", "claude"):
            code, execute = self.launch(target, connection=None)
            self.assertEqual(code, 0)
            self.assertEqual(execute.call_args.args[1], [target])
            self.assertNotIn("NAIA_MCP_TOKEN", execute.call_args.args[2])
        self.assertFalse(self.home.exists())

    def test_missing_skill_cancels_attachment(self):
        with patch.dict(self.globals, {"NAIA_SKILL": Path(self.temp.name) / "missing"}):
            code, execute = self.launch("codex")
        self.assertEqual(code, 1)
        execute.assert_not_called()
        self.assertFalse(self.home.exists())

    def test_notice_is_exact_and_skill_contains_detailed_guidance(self):
        notice = MODULE["NOTICE_FILE"].read_text().strip()
        self.assertEqual(notice, "You are attached to a Neovim instance through the NAIA MCP tool. "
                         "There is a NAIA agent skill you can use when you think the time is appropriate; "
                         "you don’t need to read it now.")
        skill = (MODULE["NAIA_SKILL"] / "SKILL.md").read_text()
        self.assertTrue(skill.startswith("---\nname: naia\n"))
        self.assertIn("name: naia", skill)
        self.assertIn("nvim_show_card", skill)
        self.assertIn("nvim_walkthrough_file_spec", skill)
        self.assertNotIn("Sidekick", skill)

    def test_opencode_session_arguments_and_literal_messages_are_preserved(self):
        for argv in ([], ["mini", "-s", "ses_existing", "--replay"], ["--continue"],
                     ["run", "--session", "ses_parent", "--fork", "review"],
                     ["run", "--model", "provider/model", "hello"]):
            with self.subTest(argv=argv):
                self.assertEqual(MODULE["build_opencode_args"](argv), argv + ["--standalone"])
        self.assertEqual(MODULE["build_opencode_args"](["run", "--", "--help"]),
                         ["run", "--standalone", "--", "--help"])
        self.assertEqual(MODULE["build_opencode_args"](["--standalone"]), ["--standalone"])

    def test_opencode_administrative_commands_do_not_receive_attachment_config(self):
        for argv in (["--help"], ["--version"], ["service", "status"], ["mcp", "list"]):
            code, execute = self.launch("opencode", argv)
            self.assertEqual(code, 0)
            self.assertEqual(execute.call_args.args[1], ["opencode", *argv])
            self.assertNotIn("OPENCODE_CONFIG_CONTENT", execute.call_args.args[2])

    def test_opencode_cannot_attach_to_another_server(self):
        for argv in (["--server", "https://example.test"], ["--server=https://example.test"]):
            with self.assertRaisesRegex(ValueError, "explicit --server"):
                MODULE["build_opencode_args"](argv)

    def test_invalid_inline_config_fails_without_launching(self):
        for raw in ("not json", "[]", "null", '{"skills":"oops"}'):
            self.env["OPENCODE_CONFIG_CONTENT"] = raw
            code, execute = self.launch("opencode")
            self.assertEqual(code, 1)
            execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
