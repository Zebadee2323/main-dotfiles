"""Mock systemd commands: these tests never install or control a real service."""
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import agent_comms as app
import linux_service as linux
import service_manager as service
from test_agent_comms import Fixture


@unittest.skipIf(os.name == "nt", "systemd backend requires POSIX")
class LinuxServiceTests(Fixture):
    def setUp(self):
        super().setUp()
        self.env = patch.dict(os.environ, {"XDG_CONFIG_HOME": str(Path(self.temp.name) / "config with spaces")})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.manager = linux.SystemdUser(self.paths)
        self.stopped = {"loaded": False, "pid": None, "state": "inactive"}
        self.running = {"loaded": True, "pid": 123, "state": "active"}

    def install(self):
        with patch.object(self.manager, "job", return_value=self.stopped), patch.object(self.manager, "run") as run:
            self.manager.install(private_dir=app.private_dir)
        return run

    def test_definition_is_private_pins_paths_and_does_not_copy_credentials(self):
        with patch.dict(os.environ, {"NAIA_MCP_TOKEN": "test-secret", "TUIOS_PANE_ID": "test-pane"}):
            run = self.install()
        text = self.manager.unit.read_text()
        self.assertIn(f"ExecStart={linux.quote(sys.executable)} {linux.quote(Path(app.__file__).resolve())}", text)
        self.assertIn(f'"--runtime-dir" {linux.quote(self.paths.runtime)} "serve" "--quiet"', text)
        self.assertIn("Restart=always\nRestartSec=10", text)
        self.assertIn("WantedBy=default.target", text)
        self.assertNotIn("test-secret", text)
        self.assertNotIn("test-pane", text)
        self.assertEqual(self.manager.unit.stat().st_mode & 0o777, 0o600)
        self.assertEqual([c.args for c in run.call_args_list], [("daemon-reload",), ("enable", linux.UNIT)])

    def test_systemd_quoting_preserves_spaces_and_escapes_special_characters(self):
        self.assertEqual(linux.quote('a b%$"\\\n\r\t'), '"a b%%$$\\"\\\\\\n\\r\\t"')

    def test_install_is_idempotent_and_refuses_foreign_definition(self):
        self.install()
        original = self.manager.unit.read_bytes()
        self.install()
        self.assertEqual(self.manager.unit.read_bytes(), original)
        self.manager.unit.write_text("[Service]\nExecStart=/some/other/program\n")
        with self.assertRaisesRegex(service.ServiceError, "not managed"):
            self.install()

    def test_custom_paths_and_symlinks_cannot_control_another_service(self):
        self.install()
        other = linux.SystemdUser(app.Paths(self.paths.config, self.paths.state / "other", self.paths.runtime))
        with self.assertRaisesRegex(service.ServiceError, "different config/state/runtime"):
            other.stop()
        self.manager.unit.unlink()
        self.manager.unit.symlink_to(self.paths.state / "missing")
        with self.assertRaisesRegex(service.ServiceError, "symlink"):
            self.install()

    def test_reinstall_changed_definition_requires_stop(self):
        self.install()
        with patch.object(self.manager, "definition", return_value=self.manager.definition() + "# changed\n"), \
             patch.object(self.manager, "job", return_value=self.running):
            with self.assertRaisesRegex(service.ServiceError, "stop it"):
                self.manager.install(private_dir=app.private_dir)

    def test_missing_service_can_skip_without_systemctl(self):
        with patch.object(self.manager, "run") as run:
            self.assertIn("skipped", self.manager.start(busy=Mock(), health=Mock(), if_installed=True))
            with self.assertRaisesRegex(service.ServiceError, "not installed"):
                self.manager.start(busy=Mock(), health=Mock())
        run.assert_not_called()

    def test_start_checks_authenticated_pid_and_does_not_restart_healthy_job(self):
        self.install()
        with patch.object(self.manager, "job", side_effect=[self.stopped, self.running]), \
             patch.object(self.manager, "run") as run:
            self.assertIn("ready", self.manager.start(busy=lambda: False, health=lambda: 123))
        run.assert_called_once_with("start", linux.UNIT)
        with patch.object(self.manager, "job", return_value=self.running), patch.object(self.manager, "run") as run:
            self.manager.start(busy=Mock(), health=lambda: 123)
        run.assert_not_called()

    def test_start_refuses_unmanaged_listener_and_readiness_timeout(self):
        self.install()
        with patch.object(self.manager, "job", return_value=self.stopped), patch.object(self.manager, "run") as run:
            with self.assertRaisesRegex(service.ServiceError, "unmanaged"):
                self.manager.start(busy=lambda: True, health=Mock())
        run.assert_not_called()
        with patch.object(self.manager, "job", return_value=self.running), \
             patch.object(linux.time, "monotonic", side_effect=[0, 0, 6]), patch.object(linux.time, "sleep"):
            with self.assertRaisesRegex(service.ServiceError, "did not become ready"):
                self.manager.start(busy=Mock(), health=lambda: 999)

    def test_job_parser_missing_bus_errors_and_foreign_loaded_unit(self):
        for code, text, pid in [(0, "LoadState=loaded\nActiveState=active\nMainPID=123\n", 123),
                                (4, "LoadState=not-found\nMainPID=0\n", None)]:
            with patch.object(self.manager, "run", return_value=subprocess.CompletedProcess([], code, text, "")):
                self.assertEqual(self.manager.job()["pid"], pid)
        for text, error, expected in [("", "Failed to connect to bus", "connect to bus"),
                                      ("LoadState=masked\n", "", "masked"),
                                      ("LoadState=loaded\nFragmentPath=/etc/systemd/user/other.service\n", "", "another definition")]:
            with patch.object(self.manager, "run", return_value=subprocess.CompletedProcess([], 1 if error else 0, text, error)):
                with self.assertRaisesRegex(service.ServiceError, expected):
                    self.manager.job()

    def test_uninstall_stops_disables_reloads_and_preserves_data(self):
        self.install()
        marker = self.paths.state / "history.jsonl"
        marker.write_text("history")
        with patch.object(self.manager, "job", return_value=self.running), patch.object(self.manager, "run") as run:
            self.manager.uninstall()
        self.assertEqual([c.args for c in run.call_args_list], [
            ("stop", linux.UNIT), ("disable", linux.UNIT), ("daemon-reload",),
        ])
        self.assertFalse(self.manager.unit.exists())
        self.assertEqual(marker.read_text(), "history")

    def test_status_reports_listener_readiness_and_unmanaged_listener(self):
        self.install()
        with patch.object(self.manager, "job", return_value=self.running):
            self.assertTrue(self.manager.status(health=lambda: 123)["ready"])
            self.assertFalse(self.manager.status(health=lambda: 999)["ready"])
        with patch.object(self.manager, "job", return_value=self.stopped):
            self.assertTrue(self.manager.status(health=lambda: None, busy=lambda: True)["unmanaged_listener"])

    def test_systemctl_errors_are_bounded_and_actionable(self):
        for error in [FileNotFoundError("systemctl"), subprocess.TimeoutExpired("systemctl", 15)]:
            with patch.object(linux.subprocess, "run", side_effect=error):
                with self.assertRaisesRegex(service.ServiceError, "systemd user session"):
                    self.manager.run("start", linux.UNIT)
        with patch.object(linux.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "denied")) as run:
            with self.assertRaisesRegex(service.ServiceError, "denied"):
                self.manager.run("start", linux.UNIT)
        self.assertEqual(run.call_args.args[0], ["systemctl", "--user", "start", linux.UNIT])
        self.assertEqual(run.call_args.kwargs["timeout"], 15)

    def test_journal_follow_uses_argv(self):
        with patch.object(linux.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
            self.manager.logs(lines=20, follow=True)
        self.assertEqual(run.call_args.args[0], [
            "journalctl", "--user", "--unit", linux.UNIT, "--lines", "20", "--no-pager", "--follow",
        ])

    def test_platform_dispatch_holds_management_lock_for_restart(self):
        events = []
        class Lock:
            def __init__(self, path):
                pass
            def __enter__(self):
                events.append("locked")
            def __exit__(self, *_):
                events.append("unlocked")
        manager = Mock()
        manager.installed.return_value = True
        manager.stop.side_effect = lambda: events.append("stop")
        manager.start.side_effect = lambda **_: events.append("start")
        with patch.object(service.sys, "platform", "linux"), patch.object(linux, "SystemdUser", return_value=manager):
            service.manage(SimpleNamespace(action="restart"), self.paths, private_dir=app.private_dir,
                           lock=Lock, busy=Mock(), health=Mock())
        self.assertEqual(events, ["locked", "stop", "start", "unlocked"])


if __name__ == "__main__":
    unittest.main()
