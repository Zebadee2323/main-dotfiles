"""Codex command hook; no network or approval changes, and no stdout on failure."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


def resolve_helper():
    # Installed plugins are copied into Codex's cache. Find the canonical program
    # through its stable launcher instead of assuming a relative checkout path.
    launcher = shutil.which("agent-comms") or shutil.which("agent-comms.cmd")
    if not launcher:
        raise RuntimeError("agent-comms launcher is not on PATH")
    helper = Path(launcher).resolve().with_name("agent_comms.py")
    if not helper.is_file():
        raise RuntimeError("agent-comms source-tree launcher is required")
    return helper


def main():
    try:
        event = json.loads(sys.stdin.read(65536))
        session_id = event.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("Missing session ID")
        command = [sys.executable, str(resolve_helper()), "session", "--harness", "codex",
                   "--session-id", session_id]
        result = subprocess.run(command, capture_output=True, encoding="utf-8", timeout=12, check=True)
        assignment = json.loads(result.stdout)
        instructions = assignment["instructions"]
        if not isinstance(instructions, str) or not instructions.strip():
            raise ValueError("Empty instructions")
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": instructions}}))
    except (OSError, ValueError, KeyError, AttributeError, RuntimeError, subprocess.SubprocessError):
        print("agent-comms: identity setup failed; continuing without communications instructions.", file=sys.stderr)
    # Advisory integration: failure must not stop the user's main task.
    return 0


if __name__ == "__main__":
    sys.exit(main())
