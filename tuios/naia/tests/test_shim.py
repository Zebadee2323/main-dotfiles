import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch


MODULE = runpy.run_path(str(Path(__file__).resolve().parents[1] / "bin/tuios-naia"))
G = MODULE["main"].__globals__
ENV = {"TUIOS_ENV": "1", "TUIOS_SESSION": "work", "TUIOS_PANE_ID": "caller"}
PANES = [
    {"window_id": "caller", "workspace": 2, "pid": 10},
    {"window_id": "editor", "workspace": 2, "pid": 20},
    {"window_id": "unrelated", "workspace": 1, "pid": 30},
]


class ShimTests(unittest.TestCase):
    def codex_shim_args(self, *args):
        # Exercise the real shell shim and Python launcher without starting Codex.
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "codex"
            binary.write_text(
                f"#!{sys.executable}\nimport json, sys\nprint(json.dumps(sys.argv[1:]))\n"
            )
            binary.chmod(0o755)
            env = {"PATH": os.pathsep.join([
                directory, str(Path(shutil.which("python3")).parent), "/usr/bin", "/bin"
            ])}
            result = subprocess.run(
                [str(MODULE["BIN"] / "codex"), *args], env=env,
                capture_output=True, text=True, check=True,
            )
            return json.loads(result.stdout)

    def test_codex_session_defaults_reach_real_cli(self):
        self.assertEqual(self.codex_shim_args("hello world"), [
            "--no-daemon", "-c", 'sandbox_mode="workspace-write"',
            "-c", "sandbox_workspace_write.network_access=true", "hello world",
        ])

    def test_codex_caller_overrides_follow_defaults(self):
        overrides = ["--sandbox", "read-only", "-c",
                     "sandbox_workspace_write.network_access=false"]
        self.assertEqual(self.codex_shim_args(*overrides)[-len(overrides):], overrides)

    def test_workspace_is_callers_not_focused(self):
        run = Mock(return_value=json.dumps({"current_workspace": 1, "windows": PANES}))
        with patch.dict(G, run=run):
            self.assertEqual(MODULE["workspace_panes"](ENV), PANES[:2])
        run.assert_called_once_with(["tuios", "list-windows", "-s", "work", "--json"])

    def test_missing_identity_does_not_query_active_session(self):
        with patch.dict(G, run=Mock(side_effect=AssertionError)):
            with self.assertRaises(MODULE["DiscoveryError"]):
                MODULE["workspace_panes"]({})

    def discover(self, probes, selection=None):
        processes = "10 1 zsh\n20 1 zsh\n21 20 nvim\n22 21 nvim\n30 1 zsh\n31 30 nvim\n"
        sockets = Mock(return_value=["/socket1", "/socket2"])
        env = dict(ENV)
        if selection:
            env["TUIOS_NAIA_PANE"] = selection
        with patch.dict(G, workspace_panes=Mock(return_value=PANES[:2]),
                        run=Mock(return_value=processes), sockets_for=sockets,
                        probe=Mock(side_effect=probes)):
            result = MODULE["discover"](env)
        self.assertEqual(set(sockets.call_args.args[0]), {21, 22})
        return result

    def test_forked_editor_and_duplicate_socket(self):
        info = {"pid": 22, "wrapper": __file__}
        self.assertEqual(self.discover([info, info]), ("/socket2", __file__, "editor"))

    def test_socket_response_from_unrelated_process_is_ignored(self):
        self.assertIsNone(self.discover([{"pid": 31, "wrapper": __file__}, None]))

    def test_multiple_editors_are_not_guessed(self):
        with self.assertRaisesRegex(MODULE["DiscoveryError"], "multiple ready editors"):
            self.discover([{"pid": 21, "wrapper": __file__}, {"pid": 22, "wrapper": __file__}])

    def test_invalid_pane_selection_fails(self):
        with self.assertRaisesRegex(MODULE["DiscoveryError"], "uniquely identify"):
            self.discover([], "unrelated")

    def test_cycle_in_process_tree(self):
        self.assertFalse(MODULE["belongs_to"](1, 9, {1: (2, "nvim"), 2: (1, "nvim")}))

    def test_path_removes_shim_and_symlink_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            alias = root / "alias"
            real = root / "real"
            alias.mkdir()
            real.mkdir()
            (alias / "opencode").symlink_to(MODULE["BIN"] / "opencode")
            binary = real / "opencode"
            binary.write_text("#!/bin/sh\n")
            binary.chmod(0o755)
            env = {"PATH": os.pathsep.join(map(str, [MODULE["BIN"], alias, real]))}
            self.assertEqual(MODULE["cli_path"]("opencode", env), (str(binary), str(real)))

    def launch(self, env, editor=None, error=None, target="opencode"):
        execute = Mock()
        discover = Mock(side_effect=error, return_value=editor)
        with patch.dict(os.environ, env, clear=True), patch.dict(
            G, cli_path=Mock(return_value=("/real/cli", "/real")),
            discover=discover, warn=Mock()
        ), patch("os.execve", execute):
            status = MODULE["main"]([target, "run", "hello world"])
        return status, execute, discover

    def test_claude_delegates_to_editor_launcher(self):
        status, execute, _ = self.launch(ENV, ("/correct", "/plugin/launcher", "editor"), target="claude")
        self.assertEqual(status, 0)
        executable, args, child = execute.call_args.args
        self.assertEqual(args, [executable, "/plugin/launcher", "claude", "run", "hello world"])
        self.assertEqual(child["NVIM"], "/correct")

    def test_claude_outside_tuios_passes_through(self):
        status, execute, discover = self.launch({}, target="claude")
        self.assertEqual(status, 0)
        discover.assert_not_called()
        self.assertEqual(execute.call_args.args[:2], ("/real/cli", ["claude", "run", "hello world"]))

    def test_outside_tuios_passes_through(self):
        _, execute, discover = self.launch({"NVIM": "/original"})
        discover.assert_not_called()
        self.assertEqual(execute.call_args.args[:2], ("/real/cli", ["opencode", "run", "hello world"]))
        self.assertEqual(execute.call_args.args[2]["NVIM"], "/original")

    def test_selected_editor_delegates_to_its_launcher(self):
        env = dict(ENV, NVIM="/wrong", NVIM_LISTEN_ADDRESS="/wrong", NAIA_MCP_TOKEN="stale")
        _, execute, _ = self.launch(env, ("/correct", "/plugin/launcher", "editor"))
        executable, args, child = execute.call_args.args
        self.assertEqual(args, [executable, "/plugin/launcher", "opencode", "run", "hello world"])
        self.assertEqual(child["NVIM"], "/correct")
        self.assertNotIn("NVIM_LISTEN_ADDRESS", child)
        self.assertNotIn("NAIA_MCP_TOKEN", child)
        self.assertEqual(child["TUIOS_PANE_ID"], "caller")

    def test_no_editor_falls_back_without_stale_credentials(self):
        _, execute, _ = self.launch(dict(ENV, NAIA_MCP_TOKEN="stale", NVIM="/wrong"))
        self.assertEqual(execute.call_args.args[0], "/real/cli")
        self.assertNotIn("NVIM", execute.call_args.args[2])
        self.assertNotIn("NAIA_MCP_TOKEN", execute.call_args.args[2])

    def test_discovery_errors_cancel_launch(self):
        status, execute, _ = self.launch(ENV, error=MODULE["DiscoveryError"]("forbidden"))
        self.assertEqual(status, 1)
        execute.assert_not_called()

    def test_explicit_selection_without_ready_editor_cancels(self):
        status, execute, _ = self.launch(dict(ENV, TUIOS_NAIA_PANE="editor"))
        self.assertEqual(status, 1)
        execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
