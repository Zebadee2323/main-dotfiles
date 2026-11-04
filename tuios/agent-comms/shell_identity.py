"""Local process ownership for callers outside tuios; no shell setup required."""
import hashlib
import json
import os
from pathlib import Path
import shlex
import socket
import subprocess

ENV_KEY = "AGENT_COMMS_SHELL_OWNER"  # Internal RPC field, never a caller override.
SHELLS = {"sh", "bash", "zsh", "fish", "dash", "ksh", "tcsh", "csh", "nu",
          "pwsh", "powershell", "cmd"}
NAMESPACE = "shell:" + hashlib.sha256(socket.gethostname().encode()).hexdigest()


def processes(run=None):
    """Return a complete process snapshot or fail, never guess on query denial."""
    run = subprocess.run if run is None else run
    try:
        if os.name == "nt":
            script = ("Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,Name,"
                      "CommandLine,@{N='Started';E={$_.CreationDate.ToUniversalTime().Ticks.ToString()}} | "
                      "ConvertTo-Json -Compress")
            result = run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                         capture_output=True, encoding="utf-8", timeout=5, check=True)
            rows = json.loads(result.stdout)
            if isinstance(rows, dict):
                rows = [rows]
            return {int(r["ProcessId"]): (int(r["ParentProcessId"]), str(r["Started"]),
                                         r["Name"], r["CommandLine"] or "") for r in rows}
        result = run(["ps", "-axo", "pid=,ppid=,lstart=,comm=,args="],
                     capture_output=True, encoding="utf-8", timeout=2, check=True,
                     env={**os.environ, "LC_ALL": "C"})
        table = {}
        for line in result.stdout.splitlines():
            if not line.strip():
                continue
            fields = line.split(None, 8)
            if len(fields) == 8:
                fields.append("")
            if len(fields) < 9:
                raise ValueError("Incomplete process snapshot")
            pid, parent = int(fields[0]), int(fields[1])
            started = " ".join(fields[2:7])
            # Linux exposes boot-relative ticks, avoiding PID reuse even within
            # one wall-clock second. macOS ps provides the process creation date.
            if os.path.isdir("/proc"):
                try:
                    stat = Path(f"/proc/{pid}/stat").read_text()
                    started += ":" + stat[stat.rfind(")") + 2:].split()[19]
                except (FileNotFoundError, ProcessLookupError):
                    continue  # A process that exited during the snapshot.
                except IndexError:
                    raise ValueError("Invalid process creation time") from None
            table[pid] = (parent, started, fields[7], fields[8])
        return table
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        raise ValueError("Cannot inspect shell processes; allow process queries before requesting an identity.") from None


def token(pid, row):
    return f"{pid}:" + hashlib.sha256(json.dumps([NAMESPACE, pid, row[1]]).encode()).hexdigest()


def is_shell(row):
    name = row[2].replace("\\", "/").rsplit("/", 1)[-1].lstrip("-").lower().removesuffix(".exe")
    if name not in SHELLS:
        return False
    try:
        args = shlex.split(row[3], posix=os.name != "nt")[1:]
    except ValueError:
        return False
    if name in {"pwsh", "powershell", "cmd"}:
        return not any(arg.lower() in ("-c", "-command", "-ec", "-encodedcommand", "/c") for arg in args)
    # Agent tools commonly spawn a fresh `zsh -lc` for each call. Walk past
    # these to the durable shell that launched the agent, rather than redrawing.
    return not any(arg.startswith("-") and not arg.startswith("--") and "c" in arg[1:] for arg in args)


def discover(pid=None):
    table = processes()
    pid, seen = os.getppid() if pid is None else pid, set()
    while pid > 1 and pid not in seen and pid in table:
        seen.add(pid)
        row = table[pid]
        if is_shell(row):
            return token(pid, row)
        pid = row[0]
    raise ValueError("No persistent parent shell found. Launch the agent from a terminal shell to request an identity.")


def owner(value=None):
    value = discover() if value is None else value
    key = hashlib.sha256(json.dumps([NAMESPACE, value]).encode()).hexdigest()
    return key, NAMESPACE, value


def inventory():
    try:
        return {token(pid, row) for pid, row in processes().items() if is_shell(row)}
    except ValueError:
        return None
