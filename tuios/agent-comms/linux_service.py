"""Opt-in systemd user service. Never use sudo or start a detached child."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

from service_manager import COMMENT, LABEL, ServiceError


UNIT = f"{LABEL}.service"


def quote(value):
    # systemd ExecStart is not a shell: escape its C strings, specifiers and
    # environment expansion independently, including paths containing spaces.
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"').replace(
        "\n", "\\n").replace("\r", "\\r").replace("\t", "\\t").replace(
        "%", "%%").replace("$", "$$") + '"'


class SystemdUser:
    def __init__(self, paths, *, home=None):
        self.paths = paths
        home = Path.home() if home is None else Path(home)
        config = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
        if not config.is_absolute():
            raise ServiceError("XDG_CONFIG_HOME must be an absolute path.")
        self.unit = config / "systemd/user" / UNIT
        self.log_dir = paths.state / "logs"

    def run(self, *args, check=True):
        try:
            result = subprocess.run(["systemctl", "--user", *args],
                                    capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ServiceError(f"systemctl --user failed: {exc}. A systemd user session is required.") from None
        if check and result.returncode:
            raise ServiceError(f"systemctl --user {args[0]} failed: {result.stderr.strip() or result.stdout.strip()}")
        return result

    def identity(self):
        return "# Paths: " + json.dumps([str(self.paths.config), str(self.paths.state), str(self.paths.runtime)])

    def definition(self):
        argv = [sys.executable, str(Path(__file__).with_name("agent_comms.py").resolve()),
                "--config", self.paths.config, "--state-dir", self.paths.state,
                "--runtime-dir", self.paths.runtime, "serve", "--quiet"]
        return (f"# {COMMENT}\n{self.identity()}\n"
                "[Unit]\nDescription=agent-comms listener\n\n"
                "[Service]\nType=simple\n"
                f"ExecStart={' '.join(quote(arg) for arg in argv)}\n"
                "Restart=always\nRestartSec=10\nTimeoutStopSec=10\n"
                "Environment=PYTHONUNBUFFERED=1\nUMask=0077\n\n"
                "[Install]\nWantedBy=default.target\n")

    def installed(self):
        if self.unit.is_symlink():
            raise ServiceError(f"Cannot manage {self.unit}: symlink")
        if not self.unit.exists():
            return False
        try:
            lines = self.unit.read_text().splitlines()
            if not lines or lines[0] != f"# {COMMENT}":
                raise ValueError("not managed by agent-comms")
            if len(lines) < 2 or lines[1] != self.identity():
                raise ValueError("different config/state/runtime paths; use the installed paths")
        except (OSError, ValueError) as exc:
            raise ServiceError(f"Cannot manage {self.unit}: {exc}") from None
        return True

    def job(self):
        result = self.run("show", UNIT, "--property=LoadState,ActiveState,MainPID,ExecMainStatus,FragmentPath", check=False)
        fields = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        if result.returncode and fields.get("LoadState") != "not-found":
            raise ServiceError(f"Cannot inspect systemd user service: {result.stderr.strip() or result.stdout.strip()}")
        if fields.get("LoadState") not in {"loaded", "not-found"}:
            raise ServiceError(f"Cannot inspect systemd user service: {fields.get('LoadState', 'missing LoadState')}")
        fragment = fields.get("FragmentPath")
        if fragment and Path(fragment) != self.unit:
            raise ServiceError(f"Refusing to manage a service from another definition: {fragment}")
        try:
            pid = int(fields.get("MainPID", "0")) or None
        except ValueError:
            raise ServiceError("Invalid MainPID from systemctl.") from None
        return {"loaded": fields["LoadState"] == "loaded", "pid": pid,
                "state": fields.get("ActiveState", "inactive"),
                "last_exit_code": fields.get("ExecMainStatus")}

    def install(self, *, private_dir):
        self.installed()  # Do not overwrite another user's definition or paths.
        data = self.definition()
        if self.unit.exists() and self.unit.read_text() == data:
            self.run("daemon-reload")
            self.run("enable", UNIT)
            return f"Service already installed: {self.unit}."
        job = self.job()
        if job["pid"] or job["state"] in {"active", "activating", "deactivating", "reloading"}:
            raise ServiceError("Service is running; stop it before reinstalling its definition.")
        self.unit.parent.mkdir(parents=True, exist_ok=True)
        private_dir(self.log_dir)
        fd, name = tempfile.mkstemp(prefix=f".{LABEL}.", dir=self.unit.parent)
        try:
            with os.fdopen(fd, "w") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.unit)
        finally:
            Path(name).unlink(missing_ok=True)
        self.run("daemon-reload")
        self.run("enable", UNIT)
        return f"Installed {self.unit}. Run 'agent-comms service start'."

    def start(self, *, busy, health, if_installed=False):
        if not self.installed():
            if if_installed:
                return "Service is not installed; skipped."
            raise ServiceError("Service is not installed. Run 'agent-comms service install' first.")
        job = self.job()
        if not job["pid"] and busy():
            raise ServiceError("An unmanaged listener is running. Stop it before starting the service; it was not terminated.")
        if not job["pid"]:
            self.run("start", UNIT)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = self.job()
            if job["pid"] and health() == job["pid"]:
                return f"Service is ready (PID {job['pid']})."
            time.sleep(0.1)
        raise ServiceError("Service did not become ready within 5 seconds. Run 'agent-comms service logs' and 'agent-comms service status'.")

    def stop(self):
        if not self.installed():
            return "Service is not installed; nothing stopped."
        self.job()  # Validate the loaded definition before controlling it.
        self.run("stop", UNIT)  # Explicit stop suppresses Restart=always.
        return "Service stopped (will start at next login or tuios attach)."

    def status(self, *, health, busy=lambda: False):
        installed = self.installed()
        job = self.job()
        listener_pid = health()
        return {"backend": "systemd", "installed": installed, **job,
                "ready": bool(job["pid"] and listener_pid == job["pid"]),
                "unmanaged_listener": bool(not job["pid"] and busy()),
                "listener_pid": listener_pid, "definition": str(self.unit),
                "logs": str(self.log_dir)}

    def uninstall(self):
        if self.installed():
            self.stop()
            self.run("disable", UNIT)
            self.unit.unlink()
            self.run("daemon-reload")
        return "Service uninstalled. History, identities, and logs were preserved."

    def logs(self, *, lines=100, follow=False):
        # Include early Python failures in the user journal as well as lifecycle
        # logs; never interpolate arguments into a shell command.
        try:
            result = subprocess.run(["journalctl", "--user", "--unit", UNIT,
                                     "--lines", str(lines), "--no-pager",
                                     *(["--follow"] if follow else [])])
        except OSError as exc:
            raise ServiceError(f"Reading service journal failed: {exc}") from None
        if result.returncode:
            raise ServiceError(f"Reading service journal failed (exit {result.returncode}).")
        return ""
