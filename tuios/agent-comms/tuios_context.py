"""Read-only, best-effort sender attribution. Never focus or manipulate panes."""
import json
import os
import shutil
import subprocess
import sys


class ContextError(Exception):
    """The sender appears to be in tuios, but its context could not be verified."""


def executable(env=None):
    """launchd omits Homebrew from PATH; resolve before running, never retry a denial."""
    env = os.environ if env is None else env
    found = shutil.which("tuios", path=env.get("PATH", os.defpath))
    if found is None and sys.platform == "darwin":
        found = shutil.which("tuios", path="/opt/homebrew/bin:/usr/local/bin")
    return found or "tuios"


def bounds(window):
    """Ignore nominal, minimized, or malformed geometry rather than invent a place."""
    if window.get("unplaced") or window.get("minimized"):
        return None
    values = [window.get(key) for key in ("x", "y", "width", "height")]
    if any(type(value) is not int for value in values) or values[2] <= 0 or values[3] <= 0:
        return None
    x, y, width, height = values
    return x, y, x + width, y + height


def relative_position(own, focused):
    a, b = bounds(own), bounds(focused)
    if a is None or b is None:
        return None
    # Shared projections are the same row/column, even with unequal pane sizes.
    horizontal = "left" if a[2] <= b[0] else "right" if a[0] >= b[2] else ""
    vertical = "top" if a[3] <= b[1] else "bottom" if a[1] >= b[3] else ""
    if not horizontal and not vertical:
        # Floating panes may overlap; compare centers in that case.
        dx, dy = a[0] + a[2] - b[0] - b[2], a[1] + a[3] - b[1] - b[3]
        horizontal = "left" if dx < 0 else "right" if dx > 0 else ""
        vertical = "top" if dy < 0 else "bottom" if dy > 0 else ""
    return "-".join(part for part in (vertical, horizontal) if part) or None


def workspace_position(own, windows):
    a = bounds(own)
    peers = [box for window in windows if window.get("workspace") == own["workspace"]
             and not window.get("scratch") and (box := bounds(window)) is not None]
    if a is None or not peers:
        return None
    left, top = min(b[0] for b in peers), min(b[1] for b in peers)
    right, bottom = max(b[2] for b in peers), max(b[3] for b in peers)
    x = ((a[0] + a[2]) / 2 - left) / (right - left)
    y = ((a[1] + a[3]) / 2 - top) / (bottom - top)
    horizontal = "left" if x < 1 / 3 else "right" if x > 2 / 3 else ""
    vertical = "top" if y < 1 / 3 else "bottom" if y > 2 / 3 else ""
    return "-".join(part for part in (vertical, horizontal) if part) or "center"


def context(name, run=subprocess.run, env=None, strict=False):
    explicit_env = env is not None
    env = os.environ if env is None else env
    pane = env.get("TUIOS_PANE_ID") or env.get("TUIOS_WINDOW_ID")
    session = env.get("TUIOS_SESSION")
    if env.get("TUIOS_ENV") != "1":
        return {}
    if not pane or not session:
        if strict:
            raise ContextError("Cannot check tuios prefix: pane/session environment is incomplete.")
        return {}

    command = "session-info"

    def query(name):
        nonlocal command
        command = name
        result = run([executable(env), command, "-s", session, "--json"], capture_output=True,
                     encoding="utf-8", timeout=2, check=True, **({"env": env} if explicit_env else {}))
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
        focused_pane = next((window for window in windows
                             if window.get("window_id") == listing.get("focused_window_id")), None)
        focused = focused_pane is not None and focused_pane.get("window_id") == pane
        # The caller is an agent even if its state marker has not caught up.
        # Only positively identified non-agent peers can establish that it is
        # alone; missing/unknown state must not suppress attribution by mistake.
        sole_agent = all(window.get("agent_state") == "none" for window in windows
                         if window.get("workspace") == workspace and window.get("window_id") != pane)
        session_name = info.get("session_name") or session
        source = {"session_name": session_name, "pane_id": pane, "workspace": workspace,
                   # Being alone only removes ambiguity in the workspace the user can see.
                   "introduce": not visible or (not sole_agent and not focused)}
        source["situation"] = ("different_session" if not info["tui_attached"] else
                               "different_workspace" if not visible else
                               "focused" if focused else "same_workspace")
        source["sole_agent"] = sole_agent
        # Keep geometry available even when the default policy suppresses an
        # introduction: a configured rule may choose to include one instead.
        absolute = workspace_position(own, windows)
        if absolute:
            source["absolute_position"] = absolute
        if visible and not focused and focused_pane is not None and focused_pane.get("workspace") == workspace:
            relative = relative_position(own, focused_pane)
            if relative:
                source["relative_position"] = relative
        # Empty slots are available by default, not additional workspaces in use.
        # Include the current workspace even when empty: the sender can be hidden.
        workspaces = [window.get("workspace") for window in windows if not window.get("scratch")]
        workspaces.append(listing["current_workspace"])
        if all(type(value) is int for value in workspaces):
            source["workspace_count"] = len(set(workspaces))
        workspace_names = info.get("workspace_names")
        if isinstance(workspace_names, dict):
            workspace_name = workspace_names.get(str(workspace))
            if isinstance(workspace_name, str) and workspace_name.strip():
                source["workspace_name"] = workspace_name
        if isinstance(own.get("display_name"), str):
            source["pane_title"] = own["display_name"]
        if source["introduce"] and visible and not focused and focused_pane is not None and focused_pane.get("workspace") == workspace:
            position = source.get("relative_position")
            if position:
                source.update(position=position, position_reference="focused-pane")
        elif source["introduce"] and not visible:
            # Detached sessions retain their layout. Describe that workspace,
            # never a direction relative to a pane the user cannot see.
            position = absolute
            if position:
                source.update(position=position, position_reference="workspace")
        return source
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        # Do not retry forbidden queries through another route. Strict senders
        # must not mistake unavailable context for a verified empty prefix.
        if strict:
            if isinstance(exc, FileNotFoundError):
                detail = "tuios executable was not found"
            elif isinstance(exc, PermissionError):
                detail = "access denied by permissions or a sandbox"
            elif isinstance(exc, subprocess.CalledProcessError):
                detail = f"tuios exited with status {exc.returncode}"
            elif isinstance(exc, subprocess.TimeoutExpired):
                detail = "tuios query timed out"
            else:
                detail = "tuios query failed or returned invalid data"
            raise ContextError(f"Cannot check tuios prefix ({command}): {detail}.") from None
        return {}
