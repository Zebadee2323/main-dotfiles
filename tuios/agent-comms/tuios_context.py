"""Read-only, best-effort sender attribution. Never focus or manipulate panes."""
import json
import os
import subprocess


class ContextError(Exception):
    """The sender appears to be in tuios, but its context could not be verified."""


def context(name, run=subprocess.run, env=None, strict=False):
    env = os.environ if env is None else env
    pane = env.get("TUIOS_PANE_ID") or env.get("TUIOS_WINDOW_ID")
    session = env.get("TUIOS_SESSION")
    if env.get("TUIOS_ENV") != "1":
        return {}
    if not pane or not session:
        if strict:
            raise ContextError("Cannot check tuios prefix: pane/session environment is incomplete.")
        return {}

    def query(command):
        result = run(["tuios", command, "-s", session, "--json"], capture_output=True,
                     encoding="utf-8", timeout=2, check=True)
        return json.loads(result.stdout)

    try:
        info = query("session-info")
        listing = query("list-windows")
        windows = listing["windows"]
        own = next((window for window in windows if window.get("window_id") == pane), None)
        if own is None:
            if strict:
                raise ContextError("Cannot check tuios prefix: sender pane was not found.")
            return {}
        workspace = own["workspace"]
        visible = bool(info["tui_attached"]) and workspace == listing["current_workspace"]
        focused = listing.get("focused_window_id") == pane
        agents = sum(1 for window in windows if window.get("workspace") == workspace and (
            window.get("window_id") == pane or window.get("agent_harness") or window.get("agent_resumable")
            or window.get("agent_state") in ("working", "idle", "needs_input", "done", "errored")
            or str(window.get("foreground_cmd", "")).replace("\\", "/").rsplit("/", 1)[-1]
            in ("opencode", "codex", "claude", "gemini", "qwen", "kilo", "aider")
        ))
        session_name = info.get("session_name") or session
        return {"session_name": session_name, "pane_id": pane, "workspace": workspace,
                "introduce": not visible or (not focused and agents > 1)}
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        # Do not retry forbidden queries through another route. Strict senders
        # must not mistake unavailable context for a verified empty prefix.
        if strict:
            raise ContextError("Cannot check tuios prefix: context query failed or was not permitted.") from None
        return {}
