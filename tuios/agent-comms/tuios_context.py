"""Read-only, best-effort sender attribution. Never focus or manipulate panes."""
import json
import os
import subprocess


class ContextError(Exception):
    """The sender appears to be in tuios, but its context could not be verified."""


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
        focused_pane = next((window for window in windows
                             if window.get("window_id") == listing.get("focused_window_id")), None)
        focused = focused_pane is not None and focused_pane.get("window_id") == pane
        session_name = info.get("session_name") or session
        source = {"session_name": session_name, "pane_id": pane, "workspace": workspace,
                  "introduce": not visible or not focused}
        if visible and not focused and focused_pane is not None and focused_pane.get("workspace") == workspace:
            position = relative_position(own, focused_pane)
            if position:
                source.update(position=position, position_reference="focused-pane")
        elif not visible and info["tui_attached"]:
            position = workspace_position(own, windows)
            if position:
                source.update(position=position, position_reference="workspace")
        return source
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        # Do not retry forbidden queries through another route. Strict senders
        # must not mistake unavailable context for a verified empty prefix.
        if strict:
            raise ContextError("Cannot check tuios prefix: context query failed or was not permitted.") from None
        return {}
