"""Portable contract tests, not evidence of native Windows execution."""
import asyncio
import base64
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch
import xml.etree.ElementTree as ET

import agent_comms as app
import service_manager
from test_agent_comms import Fixture
import windows_service as win
import windows_service_runner as runner


SID = "S-1-5-21-111-222-333-1001"
SERVICE_ID = "785d545d-7978-4f14-86c1-3852851d6b2d"


class WindowsServiceTests(Fixture):
    def setUp(self):
        super().setUp()
        self.manager = win.ScheduledTask(self.paths, probe=lambda: {"pid": 321, "service_id": SERVICE_ID})

    def job(self, *, exists=True, **kwargs):
        result = {"exists": exists, "sid": SID, "name": "agent-comms-" + SID}
        if exists:
            xml = self.manager.definition(SID, SERVICE_ID)
            metadata = json.loads(ET.fromstring(xml).findtext(f"{{{win.NS}}}RegistrationInfo/{{{win.NS}}}Description"))
            result.update(xml=xml, state=3, instances=0, enabled=True, last_result=0, metadata=metadata)
        result.update(kwargs)
        return result

    def test_xml_uses_interactive_least_privilege_singleton_and_bounded_retries(self):
        with patch.dict(os.environ, {"ELEVENLABS_API_KEY": "not-for-task", "TUIOS_PANE_ID": "not-for-task"}):
            root = ET.fromstring(self.manager.definition(SID, SERVICE_ID))
        def text(path):
            return root.findtext("/".join("{" + win.NS + "}" + name for name in path.split("/")))
        self.assertEqual(text("Principals/Principal/UserId"), SID)
        self.assertEqual(text("Principals/Principal/LogonType"), "InteractiveToken")
        self.assertEqual(text("Principals/Principal/RunLevel"), "LeastPrivilege")
        self.assertEqual(text("Triggers/LogonTrigger/UserId"), SID)
        self.assertEqual(text("Settings/MultipleInstancesPolicy"), "IgnoreNew")
        self.assertEqual(text("Settings/ExecutionTimeLimit"), "PT0S")
        self.assertEqual(text("Settings/RestartOnFailure/Interval"), "PT1M")
        self.assertEqual(text("Settings/RestartOnFailure/Count"), "255")
        self.assertEqual(text("Settings/DisallowStartIfOnBatteries"), "false")
        self.assertEqual(text("Settings/StopIfGoingOnBatteries"), "false")
        self.assertNotIn("not-for-task", ET.tostring(root, encoding="unicode"))

    def test_xml_and_windows_argv_escape_unicode_spaces_quotes_and_metacharacters(self):
        paths = app.Paths(Path('C:/Users/Ollie & Zoë/config "test".toml'), Path("C:/Space Here/state"), Path("C:/run"))
        manager = win.ScheduledTask(paths)
        with patch.object(win.sys, "executable", "C:/Python Space/python.exe"), patch.object(Path, "exists", return_value=False):
            xml = manager.definition(SID, SERVICE_ID)
        root = ET.fromstring(xml)
        command = root.findtext(f"{{{win.NS}}}Actions/{{{win.NS}}}Exec/{{{win.NS}}}Command")
        arguments = root.findtext(f"{{{win.NS}}}Actions/{{{win.NS}}}Exec/{{{win.NS}}}Arguments")
        self.assertEqual(command, "C:/Python Space/python.exe")
        self.assertIn(subprocess.list2cmdline([str(paths.config)]), arguments)
        self.assertIn("&amp;", xml)
        self.assertNotIn("cmd.exe", xml)

    def test_powershell_is_fixed_source_with_utf8_json_input_and_bounded_execution(self):
        payload = {"action": "install", "xml": "<Task>Zoë & $(); 'not-code'</Task>"}
        result = subprocess.CompletedProcess([], 0, '\ufeff{"exists":false}', "")
        with patch.object(win.subprocess, "run", return_value=result) as run:
            self.assertFalse(win.powershell(payload)["exists"])
        args = run.call_args.args[0]
        self.assertEqual(base64.b64decode(args[-1]).decode("utf-16-le"), win.BRIDGE)
        self.assertEqual(json.loads(run.call_args.kwargs["input"]), payload)
        self.assertEqual(run.call_args.kwargs["encoding"], "utf-8")
        self.assertEqual(run.call_args.kwargs["timeout"], 15)
        self.assertNotIn("-ExecutionPolicy", args)
        self.assertNotIn("shell", run.call_args.kwargs)

    def test_bridge_errors_never_become_not_installed(self):
        for error in (FileNotFoundError("powershell missing"), subprocess.TimeoutExpired("powershell", 15)):
            with patch.object(win.subprocess, "run", side_effect=error):
                with self.assertRaisesRegex(service_manager.ServiceError, "command failed"):
                    win.powershell({"action": "inspect"})
        with patch.object(win.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "access denied")):
            with self.assertRaisesRegex(service_manager.ServiceError, "access denied"):
                win.powershell({"action": "inspect"})
        with patch.object(win.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "[]", "")):
            with self.assertRaisesRegex(service_manager.ServiceError, "invalid JSON"):
                win.powershell({"action": "inspect"})

    def test_inspect_refuses_unknown_metadata_other_paths_and_other_users(self):
        with patch.object(win, "powershell", return_value=self.job()):
            self.assertEqual(self.manager.inspect()["metadata"]["service_id"], SERVICE_ID)
            other = win.ScheduledTask(app.Paths(self.paths.config, self.paths.state / "other", self.paths.runtime))
            with self.assertRaisesRegex(service_manager.ServiceError, "different state"):
                other.installed()
        for job in (self.job(xml="<not-ours/>"), self.job(sid="S-1-5-18")):
            with patch.object(win, "powershell", return_value=job):
                with self.assertRaises(service_manager.ServiceError):
                    self.manager.installed()

    def test_install_registers_only_after_inspection_without_starting(self):
        with patch.object(self.manager, "inspect", return_value=self.job(exists=False)), patch.object(win, "powershell") as bridge:
            self.manager.install(private_dir=app.private_dir)
        self.assertEqual(bridge.call_args.args[0]["action"], "install")
        self.assertIsNone(bridge.call_args.args[0]["expected"])
        self.assertTrue(self.manager.log_dir.is_dir())

    def test_running_install_is_idempotent_despite_scheduler_xml_whitespace(self):
        job = self.job(instances=1)
        root = ET.fromstring(job["xml"])
        ET.indent(root)
        job["xml"] = ET.tostring(root, encoding="unicode")
        with patch.object(self.manager, "inspect", return_value=job), patch.object(win, "powershell") as bridge:
            self.assertIn("already installed", self.manager.install(private_dir=app.private_dir))
        bridge.assert_not_called()

    def test_start_missing_skips_only_when_requested(self):
        with patch.object(self.manager, "inspect", return_value=self.job(exists=False)), patch.object(win, "powershell") as bridge:
            self.assertIn("skipped", self.manager.start(busy=Mock(), health=Mock(), if_installed=True))
            with self.assertRaisesRegex(service_manager.ServiceError, "not installed"):
                self.manager.start(busy=Mock(), health=Mock())
        bridge.assert_not_called()

    def test_start_refuses_manual_listener_and_never_restarts_running_task(self):
        with patch.object(self.manager, "inspect", return_value=self.job()), patch.object(win, "powershell") as bridge:
            with self.assertRaisesRegex(service_manager.ServiceError, "unmanaged"):
                self.manager.start(busy=lambda: True, health=Mock())
            bridge.assert_not_called()
        with patch.object(self.manager, "inspect", return_value=self.job(instances=1)), patch.object(win, "powershell") as bridge:
            self.assertIn("PID 321", self.manager.start(busy=Mock(), health=Mock()))
            bridge.assert_not_called()

    def test_start_enables_task_and_requires_authenticated_installation_identity(self):
        job = self.job(enabled=False)
        with patch.object(self.manager, "inspect", return_value=job), patch.object(win, "powershell") as bridge:
            self.manager.start(busy=lambda: False, health=Mock())
        bridge.assert_called_once_with({"action": "start", "expected": job["xml"]})
        self.manager.probe = lambda: {"pid": 321, "service_id": "other-installation"}
        with patch.object(self.manager, "inspect", return_value=job), patch.object(win, "powershell"), \
             patch.object(win.time, "monotonic", side_effect=[0, 0, 16]), patch.object(win.time, "sleep"):
            with self.assertRaisesRegex(service_manager.ServiceError, "did not become ready"):
                self.manager.start(busy=lambda: False, health=Mock())

    def test_stop_waits_for_instances_to_end_without_process_name_kills(self):
        running = self.job(instances=1)
        with patch.object(self.manager, "inspect", side_effect=[running, running, self.job(enabled=False)]), \
             patch.object(win, "powershell") as bridge, patch.object(win.time, "sleep"):
            self.assertIn("disabled", self.manager.stop())
        bridge.assert_called_once_with({"action": "stop", "expected": running["xml"]})

    def test_status_distinguishes_task_state_from_authenticated_readiness(self):
        with patch.object(self.manager, "inspect", return_value=self.job(instances=1, state=4)):
            self.assertTrue(self.manager.status(health=Mock())["ready"])
            self.manager.probe = lambda: {"pid": 123, "service_id": None}
            self.assertFalse(self.manager.status(health=Mock())["ready"])

    def test_uninstall_preserves_logs_and_uses_latest_post_stop_definition(self):
        app.private_dir(self.manager.log_dir)
        log = self.manager.log_dir / "server.log"
        log.write_text("preserve")
        stopped = self.job(enabled=False)
        with patch.object(self.manager, "installed", return_value=True), patch.object(self.manager, "stop"), \
             patch.object(self.manager, "inspect", return_value=stopped), patch.object(win, "powershell") as bridge:
            self.manager.uninstall()
        bridge.assert_called_once_with({"action": "delete", "expected": stopped["xml"]})
        self.assertEqual(log.read_text(), "preserve")

    def test_logs_are_portable_and_follow_reopens_after_rotation(self):
        app.private_dir(self.manager.log_dir)
        log = self.manager.log_dir / "server.log"
        log.write_text("old\nline two\n")
        def rotate(_):
            log.rename(log.with_suffix(".log.1"))
            log.write_text("new\n")
        with patch("sys.stdout", new_callable=io.StringIO) as output:
            self.manager.logs(lines=1)
        self.assertIn("line two", output.getvalue())
        self.assertNotIn("old\n", output.getvalue())
        with patch("sys.stdout", new_callable=io.StringIO) as output:
            calls = 0
            def step(_):
                nonlocal calls
                calls += 1
                if calls == 1:
                    rotate(0)
                else:
                    raise KeyboardInterrupt
            with patch.object(win.time, "sleep", side_effect=step):
                with self.assertRaises(KeyboardInterrupt):
                    self.manager.logs(lines=1, follow=True)
        self.assertIn("new\n", output.getvalue())

    def test_platform_dispatch_and_longer_management_lock_do_not_invoke_launchctl(self):
        manager = Mock()
        lock = MagicMock()
        args = SimpleNamespace(action="start", if_installed=True)
        with patch.object(service_manager.sys, "platform", "win32"), patch.object(win, "ScheduledTask", return_value=manager), \
             patch.object(service_manager, "LaunchAgent") as mac:
            service_manager.manage(args, self.paths, private_dir=Mock(), lock=lock, busy=Mock(), health=Mock(), probe=Mock())
        mac.assert_not_called()
        lock.assert_called_once_with(self.paths.state / "service-management.lock", timeout=60)
        manager.start.assert_called_once()


class RunnerTests(Fixture):
    def test_runner_cleans_environment_captures_errors_and_stays_in_this_pid(self):
        log = Path(self.temp.name) / "early.log"
        def execute(script, **kwargs):
            self.assertEqual(kwargs["run_name"], "__main__")
            self.assertEqual(sys.argv[-2:], ["serve", "--quiet"])
            self.assertEqual(os.environ["AGENT_COMMS_SERVICE_ID"], SERVICE_ID)
            self.assertNotIn("ELEVENLABS_API_KEY", os.environ)
            self.assertNotIn("TUIOS_PANE_ID", os.environ)
            self.assertNotIn("AGENT_COMMS_CONFIG", os.environ)
            raise RuntimeError("early startup failed")
        with patch.dict(os.environ, {"ELEVENLABS_API_KEY": "secret", "TUIOS_PANE_ID": "pane", "AGENT_COMMS_CONFIG": "old"}), \
             patch.object(runner.runpy, "run_path", side_effect=execute), patch.object(subprocess, "Popen") as child:
            self.assertEqual(runner.main(["--service-id", SERVICE_ID, "--log-file", str(log), "--", "serve", "--quiet"]), 1)
        child.assert_not_called()
        self.assertIn("early startup failed", log.read_text())
        self.assertNotIn("secret", log.read_text())

    def test_runner_preserves_exit_status_for_scheduler_retries(self):
        for code in (0, 1, 23):
            with patch.dict(os.environ), patch.object(runner.runpy, "run_path", side_effect=SystemExit(code)):
                self.assertEqual(runner.main(["--service-id", SERVICE_ID, "--log-file", str(Path(self.temp.name) / "early.log")]), code)

    def test_real_runner_listener_has_action_pid_and_service_identity(self):
        script = Path(runner.__file__).resolve()
        command = [sys.executable, str(script), "--service-id", SERVICE_ID,
                   "--log-file", str(Path(self.temp.name) / "early.log"), "--",
                   "--config", str(self.paths.config), "--state-dir", str(self.paths.state),
                   "--runtime-dir", str(self.paths.runtime), "serve", "--quiet"]
        process = subprocess.Popen(command, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self.cleanup_process, process)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if process.poll() is not None:
                self.fail((Path(self.temp.name) / "early.log").read_text())
            try:
                result = asyncio.run(app.rpc(self.paths, "ping"))
                self.assertEqual(result["pid"], process.pid)
                self.assertEqual(result["service_id"], SERVICE_ID)
                break
            except (app.CommsError, OSError):
                time.sleep(0.01)
        else:
            self.fail("Runner listener did not become ready")
