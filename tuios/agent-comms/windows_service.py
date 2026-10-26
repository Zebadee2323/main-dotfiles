"""Experimental per-user Task Scheduler backend (native Windows not yet tested)."""
from __future__ import annotations

import base64
from collections import deque
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET

from service_manager import COMMENT, ServiceError


NS = "http://schemas.microsoft.com/windows/2004/02/mit/task"

# Task Scheduler's COM API avoids parsing localized schtasks tables/errors.
# All variable data arrives as JSON on stdin, never as PowerShell source.
BRIDGE = r"""
$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
try {
    $p = [Console]::In.ReadToEnd() | ConvertFrom-Json
    $sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $name = 'agent-comms-' + $sid
    $scheduler = New-Object -ComObject 'Schedule.Service'
    $scheduler.Connect()
    $folder = $scheduler.GetFolder('\')
    $task = $null
    try { $task = $folder.GetTask($name) } catch {
        $e = $_.Exception
        while ($e.InnerException) { $e = $e.InnerException }
        if ($e.HResult -ne -2147024894) { throw }
    }
    if ($p.action -ne 'inspect') {
        if ($task) {
            if ($task.Xml -cne $p.expected) { throw 'Task changed; inspect it before retrying.' }
        } elseif ($null -ne $p.expected) {
            throw 'Task disappeared; inspect it before retrying.'
        }
        switch ($p.action) {
            'install' {
                # CREATE or UPDATE, not CREATE_OR_UPDATE: refuse an external race.
                $flags = 2
                if ($task) { $flags = 4 }
                $task = $folder.RegisterTask($name, $p.xml, $flags, $sid, $null, 3, $null)
            }
            'start' {
                if (-not $task) { throw 'Task is not installed.' }
                $task.Enabled = $true
                if ($task.GetInstances(0).Count -eq 0) { $null = $task.Run($null) }
            }
            'stop' {
                if (-not $task) { throw 'Task is not installed.' }
                # Disable first: failure recovery/logon must not undo a stop.
                $task.Enabled = $false
                if ($task.GetInstances(0).Count -gt 0) { $task.Stop(0) }
            }
            'delete' { $folder.DeleteTask($name, 0); $task = $null }
            default { throw 'Unknown scheduler operation.' }
        }
    }
    $result = @{ exists = [bool]$task; sid = $sid; name = $name }
    if ($task) {
        $result.xml = $task.Xml
        $result.state = [int]$task.State
        $result.instances = [int]($task.GetInstances(0).Count)
        $result.enabled = [bool]$task.Enabled
        $result.last_result = [long]$task.LastTaskResult
    }
    $result | ConvertTo-Json -Compress -Depth 8
} catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 1
}
"""


def powershell(payload):
    executable = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    command = [str(executable), "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand",
               base64.b64encode(BRIDGE.encode("utf-16-le")).decode("ascii")]
    try:
        result = subprocess.run(command, input=json.dumps(payload, ensure_ascii=False), capture_output=True,
                                text=True, encoding="utf-8", timeout=15,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ServiceError(f"Windows Task Scheduler command failed: {exc}") from None
    if result.returncode:
        raise ServiceError(f"Windows Task Scheduler failed: {result.stderr.strip() or 'no diagnostic output'}. "
                           "Check Task Scheduler availability and your user policy; no elevation was attempted.")
    try:
        value = json.loads(result.stdout.lstrip("\ufeff"))
        if not isinstance(value, dict) or type(value.get("exists")) is not bool:
            raise ValueError
        return value
    except ValueError:
        raise ServiceError("Windows Task Scheduler returned invalid JSON.") from None


class ScheduledTask:
    def __init__(self, paths, *, probe=None):
        self.paths = paths
        self.log_dir = paths.state / "logs"
        self.probe = probe or (lambda: None)

    def inspect(self):
        job = powershell({"action": "inspect"})
        if not job["exists"]:
            return job
        try:
            root = ET.fromstring(job["xml"])
            metadata = json.loads(root.findtext(f"{{{NS}}}RegistrationInfo/{{{NS}}}Description", ""))
            if metadata["managed_by"] != COMMENT:
                raise ValueError("not managed by agent-comms")
            for name in ("config", "state", "runtime"):
                if metadata[name] != str(getattr(self.paths, name)):
                    raise ValueError(f"different {name} path; use the installed paths")
            if not isinstance(metadata["service_id"], str) or str(uuid.UUID(metadata["service_id"])) != metadata["service_id"]:
                raise ValueError("invalid service identity")
            principal = root.findtext(f"{{{NS}}}Principals/{{{NS}}}Principal/{{{NS}}}UserId")
            if principal != job["sid"]:
                raise ValueError("task belongs to a different user")
            job["metadata"] = metadata
        except (ValueError, KeyError, TypeError, ET.ParseError) as exc:
            raise ServiceError(f"Cannot manage scheduled task {job.get('name')}: {exc}") from None
        return job

    def installed(self):
        return self.inspect()["exists"]

    def definition(self, sid, service_id):
        # XML escaping and Windows CRT argv quoting are separate requirements.
        root = ET.Element("Task", {"xmlns": NS, "version": "1.3"})
        def node(parent, name, value=None, **attributes):
            child = ET.SubElement(parent, name, attributes)
            if value is not None:
                child.text = str(value)
            return child
        info = node(root, "RegistrationInfo")
        metadata = {"managed_by": COMMENT, "service_id": service_id,
                    **{key: str(getattr(self.paths, key)) for key in ("config", "state", "runtime")}}
        node(info, "Description", json.dumps(metadata, ensure_ascii=False))
        trigger = node(node(root, "Triggers"), "LogonTrigger")
        node(trigger, "Enabled", "true")
        node(trigger, "UserId", sid)
        principal = node(node(root, "Principals"), "Principal", id="User")
        node(principal, "UserId", sid)
        node(principal, "LogonType", "InteractiveToken")
        node(principal, "RunLevel", "LeastPrivilege")
        settings = node(root, "Settings")
        for key, value in {"MultipleInstancesPolicy": "IgnoreNew", "DisallowStartIfOnBatteries": "false",
                           "StopIfGoingOnBatteries": "false", "AllowHardTerminate": "true",
                           "StartWhenAvailable": "true", "RunOnlyIfNetworkAvailable": "false",
                           "AllowStartOnDemand": "true", "Enabled": "true", "Hidden": "true",
                           "RunOnlyIfIdle": "false", "ExecutionTimeLimit": "PT0S"}.items():
            node(settings, key, value)
        restart = node(settings, "RestartOnFailure")
        node(restart, "Interval", "PT1M")  # Scheduler schema minimum, not 10 seconds.
        node(restart, "Count", 255)
        action = node(node(root, "Actions", Context="User"), "Exec")
        interpreter = Path(sys.executable)
        windowless = interpreter.with_name("pythonw.exe")
        if windowless.exists():
            interpreter = windowless
        node(action, "Command", interpreter)
        args = [str(Path(__file__).with_name("windows_service_runner.py").resolve()),
                "--service-id", service_id, "--log-file", str(self.log_dir / "windows.stderr.log"), "--",
                "--config", str(self.paths.config), "--state-dir", str(self.paths.state),
                "--runtime-dir", str(self.paths.runtime), "serve", "--quiet"]
        node(action, "Arguments", subprocess.list2cmdline(args))
        node(action, "WorkingDirectory", Path(__file__).resolve().parent)
        return ET.tostring(root, encoding="unicode")

    def install(self, *, private_dir):
        job = self.inspect()
        service_id = job["metadata"]["service_id"] if job["exists"] else str(uuid.uuid4())
        xml = self.definition(job["sid"], service_id)
        if job["exists"]:
            # Scheduler normalizes XML. Compare the action, not raw serialization.
            def action_values(value):
                root = ET.fromstring(value)
                prefix = f"{{{NS}}}Actions/{{{NS}}}Exec/"
                return tuple(root.findtext(prefix + f"{{{NS}}}{name}")
                             for name in ("Command", "Arguments", "WorkingDirectory"))
            if job.get("instances", 0):
                if action_values(job["xml"]) == action_values(xml):
                    return "Windows service already installed and running."
                raise ServiceError("Task is running; stop the service before reinstalling it.")
        private_dir(self.log_dir)
        powershell({"action": "install", "expected": job.get("xml"), "xml": xml})
        return "Installed per-user Windows scheduled task. Run 'agent-comms service start'."

    def start(self, *, busy, health, if_installed=False):
        job = self.inspect()
        if not job["exists"]:
            if if_installed:
                return "Service is not installed; skipped."
            raise ServiceError("Service is not installed. Run 'agent-comms service install' first.")
        if not job.get("instances", 0) and busy():
            raise ServiceError("An unmanaged listener is running. Stop it first; it was not terminated.")
        if not job.get("instances", 0) or not job.get("enabled"):
            powershell({"action": "start", "expected": job["xml"]})
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            response = self.probe()
            if response and response.get("service_id") == job["metadata"]["service_id"]:
                if type(response.get("pid")) is int and response["pid"] > 0:
                    return f"Service is ready (PID {response['pid']})."
            time.sleep(0.2)
        raise ServiceError("Windows service did not become ready within 15 seconds. Inspect service status/logs and Task Scheduler history.")

    def stop(self):
        job = self.inspect()
        if not job["exists"]:
            return "Service is not installed; nothing stopped."
        powershell({"action": "stop", "expected": job["xml"]})
        deadline = time.monotonic() + 15
        while self.inspect().get("instances", 0):
            if time.monotonic() >= deadline:
                raise ServiceError("Windows task has not stopped after 15 seconds; no process-name kill was attempted.")
            time.sleep(0.2)
        return "Windows service stopped and disabled. Start it again to re-enable sign-in startup (tuios attach also starts it)."

    def status(self, *, health, busy=lambda: False):
        job = self.inspect()
        response = self.probe()
        ready = bool(job["exists"] and job.get("instances") and response
                     and response.get("service_id") == job["metadata"]["service_id"]
                     and type(response.get("pid")) is int and response["pid"] > 0)
        return {"backend": "windows-task-scheduler", "installed": job["exists"],
                "loaded": job["exists"], "enabled": job.get("enabled", False),
                "state": {0: "unknown", 1: "disabled", 2: "queued", 3: "ready", 4: "running"}.get(job.get("state"), "not installed"),
                "pid": response["pid"] if ready else None, "ready": ready,
                "unmanaged_listener": bool(not job.get("instances") and busy()),
                "listener_pid": response.get("pid") if response else None,
                "last_exit_code": job.get("last_result"), "definition": job["name"], "logs": str(self.log_dir)}

    def uninstall(self):
        if self.installed():
            self.stop()
            job = self.inspect()
            powershell({"action": "delete", "expected": job["xml"]})
        return "Windows service uninstalled. History, identities, and logs were preserved."

    def logs(self, *, lines=100, follow=False):
        files = [self.log_dir / "server.log", self.log_dir / "windows.stderr.log"]
        offsets = {}
        found = False
        while True:
            for path in files:
                try:
                    with path.open("rb") as stream:
                        stat = os.fstat(stream.fileno())
                        identity, position = offsets.get(path, (None, 0))
                        same = identity == (stat.st_dev, stat.st_ino) and stat.st_size >= position
                        if same:
                            stream.seek(position)
                            data = stream.read()
                        else:
                            print(f"==> {path} <==", flush=True)
                            data = b"".join(deque(stream, maxlen=lines))
                        offsets[path] = ((stat.st_dev, stat.st_ino), stream.tell())
                    found = True
                    if data:
                        print(data.decode("utf-8", errors="replace"), end="", flush=True)
                except FileNotFoundError:
                    continue
            # Close handles between iterations so Windows can rotate the logs.
            if not follow:
                return "" if found else "No service logs yet."
            time.sleep(0.3)
