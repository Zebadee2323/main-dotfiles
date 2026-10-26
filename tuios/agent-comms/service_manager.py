"""Per-user OS supervision. Never host the server in an agent-owned process."""
from __future__ import annotations

import os
from pathlib import Path
import plistlib
import re
import subprocess
import sys
import time


LABEL = "com.agent-comms.server"
COMMENT = "Managed by agent-comms service install."


class ServiceError(Exception):
    pass


class LaunchAgent:
    def __init__(self, paths, *, home=None):
        self.paths = paths
        self.home = Path.home() if home is None else Path(home)
        self.plist = self.home / "Library/LaunchAgents" / f"{LABEL}.plist"
        self.domain = f"gui/{os.getuid()}"
        self.target = f"{self.domain}/{LABEL}"
        self.log_dir = paths.state / "logs"

    def run(self, *args, check=True):
        try:
            result = subprocess.run(["/bin/launchctl", *args], capture_output=True,
                                    text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ServiceError(f"launchctl failed: {exc}") from None
        if check and result.returncode:
            raise ServiceError(f"launchctl {args[0]} failed: {result.stderr.strip() or result.stdout.strip()}")
        return result

    def definition(self):
        # Pin the actual interpreter: launchd does not inherit the shell's PATH.
        # Do not copy the caller's environment (credentials or pane ownership).
        return {
            "Label": LABEL,
            "Comment": COMMENT,
            "ProgramArguments": [sys.executable, str(Path(__file__).with_name("agent_comms.py").resolve()),
                                 "--config", str(self.paths.config),
                                 "--state-dir", str(self.paths.state),
                                 "--runtime-dir", str(self.paths.runtime), "serve", "--quiet"],
            "RunAtLoad": True,
            "KeepAlive": True,
            "ThrottleInterval": 10,
            "ExitTimeOut": 10,
            "EnvironmentVariables": {"PYTHONUNBUFFERED": "1"},
            "StandardOutPath": str(self.log_dir / "launchd.stdout.log"),
            "StandardErrorPath": str(self.log_dir / "launchd.stderr.log"),
        }

    def installed(self):
        if not self.plist.exists():
            return False
        try:
            if self.plist.is_symlink():
                raise ValueError("symlink")
            with self.plist.open("rb") as stream:
                value = plistlib.load(stream)
            if value.get("Label") != LABEL or value.get("Comment") != COMMENT:
                raise ValueError("not managed by agent-comms")
            # One per-user job: an invocation with test/custom paths must not
            # accidentally stop or restart a service for different paths.
            args = value["ProgramArguments"]
            for flag, path in (("--config", self.paths.config), ("--state-dir", self.paths.state),
                               ("--runtime-dir", self.paths.runtime)):
                if args[args.index(flag) + 1] != str(path):
                    raise ValueError(f"different {flag}; use the installed paths")
        except (OSError, ValueError, KeyError, IndexError, TypeError, plistlib.InvalidFileException) as exc:
            raise ServiceError(f"Cannot manage {self.plist}: {exc}") from None
        return True

    def job(self):
        result = self.run("print", self.target, check=False)
        if result.returncode:
            if "could not find service" in result.stderr.lower():
                return {"loaded": False, "pid": None, "state": "stopped"}
            raise ServiceError(f"Cannot inspect launchd job: {result.stderr.strip()}")
        pid = re.search(r"^\s*pid = (\d+)\s*$", result.stdout, re.M)
        state = re.search(r"^\s*state = (.+)$", result.stdout, re.M)
        exit_code = re.search(r"^\s*last exit code = (.+)$", result.stdout, re.M)
        return {"loaded": True, "pid": int(pid[1]) if pid else None,
                "state": state[1] if state else "loaded",
                "last_exit_code": exit_code[1] if exit_code else None}

    def install(self, *, private_dir):
        self.installed()  # Refuse to overwrite an unrelated file.
        data = plistlib.dumps(self.definition())
        if self.plist.exists() and self.plist.read_bytes() == data:
            return f"Service already installed: {self.plist}."
        if self.job()["loaded"]:
            raise ServiceError("Service is loaded; stop it before reinstalling its definition.")
        self.plist.parent.mkdir(parents=True, exist_ok=True)
        private_dir(self.log_dir)
        # Atomic replacement, keeping any previous definition on write failure.
        import tempfile
        fd, name = tempfile.mkstemp(prefix=f".{LABEL}.", dir=self.plist.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.plist)
        finally:
            Path(name).unlink(missing_ok=True)
        return f"Installed {self.plist}. Run 'agent-comms service start'."

    def start(self, *, busy, health, if_installed=False):
        if not self.installed():
            if if_installed:
                return "Service is not installed; skipped."
            raise ServiceError("Service is not installed. Run 'agent-comms service install' first.")
        job = self.job()
        if not job["pid"] and busy():
            raise ServiceError("An unmanaged listener is running. Stop it before starting the service; it was not terminated.")
        if not job["loaded"]:
            self.run("enable", self.target)
            self.run("bootstrap", self.domain, str(self.plist))
        elif not job["pid"]:
            self.run("kickstart", self.target)  # No -k: never kill a healthy instance.
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
        if self.job()["loaded"]:
            # Unload rather than signal: KeepAlive must not undo an explicit stop.
            self.run("bootout", self.target)
            # bootout can return before the job has disappeared. Do not let a
            # following start mistake that dying PID for an already-running job.
            deadline = time.monotonic() + 10
            while self.job()["loaded"]:
                if time.monotonic() >= deadline:
                    raise ServiceError("Service is still unloading after 10 seconds; inspect service status/logs before retrying.")
                time.sleep(0.1)
        return "Service stopped (will start at next login or tuios attach)."

    def status(self, *, health, busy=lambda: False):
        installed = self.installed()
        job = self.job()
        listener_pid = health()
        return {"backend": "launchd", "installed": installed, **job,
                "ready": bool(job["pid"] and listener_pid == job["pid"]),
                "unmanaged_listener": bool(not job["pid"] and busy()),
                "listener_pid": listener_pid, "definition": str(self.plist),
                "logs": str(self.log_dir)}

    def uninstall(self):
        if self.installed():
            self.stop()
            self.plist.unlink()
        return "Service uninstalled. History, identities, and logs were preserved."

    def logs(self, *, lines=100, follow=False):
        files = [p for p in (self.log_dir / "server.log", self.log_dir / "launchd.stderr.log") if p.exists()]
        if not files:
            return "No service logs yet."
        # Inherit output, and never invoke a shell. tail handles rotation with -F.
        result = subprocess.run(["/usr/bin/tail", "-n", str(lines), *(["-F"] if follow else []),
                                 *(str(p) for p in files)])
        if result.returncode:
            raise ServiceError(f"Reading logs failed (exit {result.returncode}).")
        return ""


def manage(args, paths, *, private_dir, lock, busy, health, probe=None):
    if sys.platform not in ("darwin", "win32"):
        if args.action == "start" and args.if_installed:
            return "Service backend unavailable on this platform; skipped."
        raise ServiceError("Service management supports macOS launchd and Windows Task Scheduler. Use 'serve' on other platforms.")
    if sys.platform == "win32":
        from windows_service import ScheduledTask
        manager = ScheduledTask(paths, probe=probe)
    else:
        manager = LaunchAgent(paths)
    if args.action == "status":
        return manager.status(health=health, busy=busy)
    if args.action == "logs":
        return manager.logs(lines=args.lines, follow=args.follow)
    private_dir(paths.state)
    management_lock = (lock(paths.state / "service-management.lock", timeout=60) if sys.platform == "win32"
                       else lock(paths.state / "service-management.lock"))
    with management_lock:
        if args.action == "install":
            return manager.install(private_dir=private_dir)
        if args.action == "start":
            return manager.start(busy=busy, health=health, if_installed=args.if_installed)
        if args.action == "stop":
            return manager.stop()
        if args.action == "restart":
            if not manager.installed():
                raise ServiceError("Service is not installed. Run 'agent-comms service install' first.")
            manager.stop()
            return manager.start(busy=busy, health=health)
        if args.action == "uninstall":
            return manager.uninstall()
        raise ServiceError(f"Unknown service action: {args.action}")
