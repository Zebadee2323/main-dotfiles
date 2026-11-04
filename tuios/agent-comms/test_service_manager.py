"""Supervisor commands are mocked; no tests install or control a real service."""
import asyncio
import io
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import agent_comms as app
import service_manager as service
from test_agent_comms import Fixture


@unittest.skipIf(os.name == "nt", "LaunchAgent tests require POSIX; Windows backend has separate contract tests")
class ServiceTests(Fixture):
    def setUp(self):
        super().setUp()
        self.manager = service.LaunchAgent(self.paths, home=Path(self.temp.name) / "home with spaces")
        self.stopped = {"loaded": False, "pid": None, "state": "stopped"}
        self.running = {"loaded": True, "pid": 123, "state": "running"}

    def install(self):
        with patch.object(self.manager, "job", return_value=self.stopped):
            return self.manager.install(private_dir=app.private_dir)

    def test_definition_uses_absolute_argv_and_no_caller_credentials(self):
        with patch.dict(os.environ, {"ELEVENLABS_API_KEY": "test-secret", "TUIOS_PANE_ID": "test-pane"}):
            self.install()
        data = plistlib.loads(self.manager.plist.read_bytes())
        self.assertEqual(data["ProgramArguments"][:2], [sys.executable, str(app.Path(app.__file__).resolve())])
        self.assertEqual(data["ProgramArguments"][-2:], ["serve", "--quiet"])
        self.assertTrue(data["RunAtLoad"])
        self.assertTrue(data["KeepAlive"])
        self.assertEqual(data["EnvironmentVariables"], {"PYTHONUNBUFFERED": "1"})
        self.assertNotIn(b"test-secret", self.manager.plist.read_bytes())
        self.assertEqual(self.manager.plist.stat().st_mode & 0o777, 0o600)

    def test_install_idempotent_but_refuses_unrelated_or_loaded_definition(self):
        self.install()
        original = self.manager.plist.read_bytes()
        self.install()
        self.assertEqual(self.manager.plist.read_bytes(), original)
        with patch.object(self.manager, "job", return_value=self.running), \
             patch.object(self.manager, "definition", return_value={**self.manager.definition(), "ThrottleInterval": 20}):
            with self.assertRaisesRegex(service.ServiceError, "stop it"):
                self.manager.install(private_dir=app.private_dir)
        self.manager.plist.write_bytes(plistlib.dumps({"Label": "someone-else"}))
        with self.assertRaisesRegex(service.ServiceError, "Cannot manage"):
            self.install()
        self.assertEqual(plistlib.loads(self.manager.plist.read_bytes())["Label"], "someone-else")

    def test_custom_paths_cannot_control_another_service(self):
        self.install()
        other = service.LaunchAgent(app.Paths(self.paths.config, self.paths.state / "other", self.paths.runtime),
                                    home=self.manager.home)
        with self.assertRaisesRegex(service.ServiceError, "different --state-dir"):
            other.stop()

    def test_start_missing_is_noop_only_with_if_installed(self):
        with patch.object(self.manager, "run") as run:
            self.assertIn("skipped", self.manager.start(busy=Mock(), health=Mock(), if_installed=True))
            with self.assertRaisesRegex(service.ServiceError, "not installed"):
                self.manager.start(busy=Mock(), health=Mock())
        run.assert_not_called()
        self.assertFalse(self.manager.plist.exists())

    def test_start_bootstraps_then_checks_authenticated_pid(self):
        self.install()
        with patch.object(self.manager, "job", side_effect=[self.stopped, self.running]), \
             patch.object(self.manager, "run") as run:
            self.assertIn("ready", self.manager.start(busy=lambda: False, health=lambda: 123))
        self.assertEqual([c.args[0] for c in run.call_args_list], ["enable", "bootstrap"])
        self.assertEqual(run.call_args_list[1].args, ("bootstrap", self.manager.domain, str(self.manager.plist)))

    def test_start_running_never_restarts_or_kills_it(self):
        self.install()
        with patch.object(self.manager, "job", return_value=self.running), patch.object(self.manager, "run") as run:
            for _ in range(2):
                self.manager.start(busy=Mock(), health=lambda: 123)
        run.assert_not_called()

    def test_start_refuses_manual_listener_and_readiness_timeout(self):
        self.install()
        with patch.object(self.manager, "job", return_value=self.stopped), patch.object(self.manager, "run") as run:
            with self.assertRaisesRegex(service.ServiceError, "unmanaged"):
                self.manager.start(busy=lambda: True, health=Mock())
            run.assert_not_called()
        with patch.object(self.manager, "job", return_value=self.running), \
             patch.object(service.time, "monotonic", side_effect=[0, 0, 6]), patch.object(service.time, "sleep"):
            with self.assertRaisesRegex(service.ServiceError, "did not become ready"):
                self.manager.start(busy=Mock(), health=lambda: 999)

    def test_stop_unloads_instead_of_killing_and_uninstall_preserves_data(self):
        self.install()
        marker = self.paths.state / "history.jsonl"
        marker.write_text("history")
        with patch.object(self.manager, "job", side_effect=[self.running, self.running, self.stopped]), \
             patch.object(service.time, "sleep"), patch.object(self.manager, "run") as run:
            self.manager.uninstall()
        run.assert_called_once_with("bootout", self.manager.target)
        self.assertFalse(self.manager.plist.exists())
        self.assertEqual(marker.read_text(), "history")

    def test_stop_timeout_is_not_reported_as_success(self):
        self.install()
        with patch.object(self.manager, "job", return_value=self.running), patch.object(self.manager, "run"), \
             patch.object(service.time, "monotonic", side_effect=[0, 11]):
            with self.assertRaisesRegex(service.ServiceError, "still unloading"):
                self.manager.stop()

    def test_job_parser_distinguishes_absent_service_from_errors(self):
        with patch.object(self.manager, "run", return_value=subprocess.CompletedProcess([], 0,
                          "state = running\n\tpid = 123\nlast exit code = 0\n", "")):
            self.assertEqual(self.manager.job()["pid"], 123)
        with patch.object(self.manager, "run", return_value=subprocess.CompletedProcess([], 113, "", "Could not find service")):
            self.assertFalse(self.manager.job()["loaded"])
        with patch.object(self.manager, "run", return_value=subprocess.CompletedProcess([], 1, "", "Permission denied")):
            with self.assertRaisesRegex(service.ServiceError, "Permission denied"):
                self.manager.job()

    def test_status_reports_readiness_separately_from_job_presence(self):
        self.install()
        with patch.object(self.manager, "job", return_value=self.running):
            self.assertTrue(self.manager.status(health=lambda: 123)["ready"])
            self.assertFalse(self.manager.status(health=lambda: None)["ready"])
        with patch.object(self.manager, "job", return_value=self.stopped):
            self.assertTrue(self.manager.status(health=lambda: None, busy=lambda: True)["unmanaged_listener"])

    def test_start_kickstarts_loaded_stopped_job_without_force(self):
        self.install()
        waiting = {"loaded": True, "pid": None, "state": "waiting"}
        with patch.object(self.manager, "job", side_effect=[waiting, self.running]), patch.object(self.manager, "run") as run:
            self.manager.start(busy=lambda: False, health=lambda: 123)
        run.assert_called_once_with("kickstart", self.manager.target)

    def test_follow_logs_uses_argv_and_never_a_shell(self):
        self.install()
        (self.manager.log_dir / "server.log").touch()
        with patch.object(service.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
            self.manager.logs(lines=20, follow=True)
        self.assertEqual(run.call_args.args[0], ["/usr/bin/tail", "-n", "20", "-F", str(self.manager.log_dir / "server.log")])

    def test_mutations_hold_management_lock_and_restart_stops_first(self):
        events = []
        class Lock:
            def __init__(self, path):
                self.path = path
            def __enter__(self):
                events.append("locked")
            def __exit__(self, *_):
                events.append("unlocked")
        manager = Mock()
        manager.installed.return_value = True
        manager.stop.side_effect = lambda: events.append("stop")
        manager.start.side_effect = lambda **_: events.append("start")
        with patch.object(service.sys, "platform", "darwin"), patch.object(service, "LaunchAgent", return_value=manager):
            service.manage(SimpleNamespace(action="restart"), self.paths, private_dir=app.private_dir,
                           lock=Lock, busy=Mock(), health=Mock())
        self.assertEqual(events, ["locked", "stop", "start", "unlocked"])

    def test_tuios_requests_start_only_at_attach_without_implicit_install(self):
        import tomllib
        config = tomllib.loads((Path(app.__file__).parent.parent / "config.toml").read_text())
        self.assertEqual(config["hooks"]["after-attach"], ['"$HOME/.local/bin/agent-comms" service start --if-installed'])

    def test_command_errors_are_actionable_and_bounded(self):
        with patch.object(service.subprocess, "run", side_effect=subprocess.TimeoutExpired("launchctl", 10)):
            with self.assertRaisesRegex(service.ServiceError, "launchctl failed"):
                self.manager.run("bootstrap", "gui/1", "file with spaces")
        with patch.object(service.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "bad job")) as run:
            with self.assertRaisesRegex(service.ServiceError, "bad job"):
                self.manager.run("bootstrap", "gui/1", "file with spaces")
            self.assertEqual(run.call_args.kwargs["timeout"], 10)

    def test_cli_options_work_at_all_levels_and_without_identity_config(self):
        for argv in (["--state-dir", "a", "service", "status"],
                     ["service", "--state-dir", "a", "status"],
                     ["service", "status", "--state-dir", "a"]):
            self.assertEqual(app.parser().parse_args(argv).state_dir, "a")
        with patch.object(service, "manage", return_value={"ready": False}) as manage, \
             patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(app.main(["--config", "/nonexistent", "service", "status"]), 0)
            self.assertFalse(json.loads(output.getvalue())["ready"])
        manage.assert_called_once()

    def test_unsupported_platform_is_explicit_but_hook_can_skip(self):
        args = SimpleNamespace(action="start", if_installed=True)
        with patch.object(service.sys, "platform", "linux"):
            self.assertIn("skipped", service.manage(args, self.paths, private_dir=Mock(), lock=Mock(), busy=Mock(), health=Mock()))
            args.if_installed = False
            with self.assertRaisesRegex(service.ServiceError, "macOS"):
                service.manage(args, self.paths, private_dir=Mock(), lock=Mock(), busy=Mock(), health=Mock())

    def test_health_endpoint_and_lifecycle_logs_with_real_isolated_listener(self):
        process, info = self.start_server()
        result = asyncio.run(app.rpc(self.paths, "ping"))
        self.assertEqual(result["pid"], process.pid)
        process.terminate()
        self.assertEqual(process.wait(timeout=5), 0)
        log = (self.paths.state / "logs/server.log").read_text()
        self.assertIn("Listener starting", log)
        self.assertIn("Listener ready", log)
        self.assertIn("Shutdown requested: SIGTERM", log)
        self.assertIn("Listener stopped", log)
        self.assertNotIn(info["token"], log)
