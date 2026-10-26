"""Durable pane bindings and conservative, daemon-scoped garbage collection."""
import hashlib
import json
import math
import os
import subprocess
import time

GRACE_SECONDS = 7 * 24 * 3600
SCAN_INTERVAL = 3600


def owner(env=None):
    env = os.environ if env is None else env
    if env.get("TUIOS_ENV") != "1":
        return None
    pane = env.get("TUIOS_PANE_ID") or env.get("TUIOS_WINDOW_ID")
    if not pane:
        raise ValueError("tuios pane ID is missing")
    # Socket path, never PID/token: stable across daemon and machine restarts.
    daemon = [env.get("TUIOS_HOST", "local"), env.get("TUIOS_SOCKET") or env.get("TUIOS_SERVER", "default")]
    daemon_key = hashlib.sha256(json.dumps(daemon).encode()).hexdigest()
    key = hashlib.sha256(json.dumps([daemon_key, pane]).encode()).hexdigest()
    return key, daemon_key, pane


def inventory(run=subprocess.run):
    """Only a complete successful listing is evidence that a pane is gone."""
    try:
        result = run(["tuios", "list-windows", "--all", "--json"], capture_output=True,
                     encoding="utf-8", timeout=2, check=True)
        data = json.loads(result.stdout)
        panes = data["panes"]
        if (data.get("success") is not True or data.get("errors") or not isinstance(panes, list)
                or type(data.get("total")) is not int or data["total"] != len(panes)
                or any(not isinstance(p, dict) or not isinstance(p.get("window_id"), str) for p in panes)):
            return None
        return {p["window_id"] for p in panes}
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        return None


def prune(state, daemon, own, live, now):
    if live is None or own not in live:
        return
    for key, record in list(state["bindings"].items()):
        if record["daemon"] != daemon:
            continue
        if record["pane_id"] in live:
            record.pop("missing_since", None)
        elif "missing_since" not in record:
            record["missing_since"] = now
        elif now - record["missing_since"] >= GRACE_SECONDS:
            del state["bindings"][key]


def timestamp(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def resolve(paths, config, *, private_dir, lock, atomic_json, draw_id, error, allocate=True, env=None):
    identity = owner(env)
    if identity is None:
        return None
    key, daemon, pane = identity
    directory = private_dir(paths.state)
    filename = directory / "pane-identities.json"
    with lock(directory / "pane-identities.lock"):
        try:
            state = json.loads(filename.read_text(encoding="utf-8"))
        except FileNotFoundError:
            state = {"version": 1, "bindings": {}, "scanned": {}}
        except (ValueError, UnicodeError):
            raise error(f"Corrupt pane identity mapping: {filename}. Repair it explicitly.") from None
        if (not isinstance(state, dict) or state.get("version") != 1
                or not isinstance(state.get("bindings"), dict) or not isinstance(state.get("scanned"), dict)
                or any(not isinstance(k, str) or not timestamp(v) for k, v in state["scanned"].items())
                or any(not isinstance(k, str) or not isinstance(r, dict)
                       or any(not isinstance(r.get(f), str) for f in ("daemon", "pane_id", "id"))
                       or ("missing_since" in r and not timestamp(r["missing_since"]))
                       for k, r in state["bindings"].items())):
            raise error(f"Invalid pane identity mapping: {filename}.")
        saved = state["bindings"].get(key)
        if not allocate:
            if saved is None or saved["id"] not in config["identities"]:
                raise error("Pane has no current identity. Run 'agent-comms context' before sending.")
            return saved["id"]
        now = time.time()
        if now - state["scanned"].get(daemon, 0) >= SCAN_INTERVAL:
            live = inventory()
            prune(state, daemon, pane, live, now)
            # Throttle failures too; never retry a forbidden query via another route.
            state["scanned"][daemon] = now
        if saved is None or saved["id"] not in config["identities"]:
            used = {r["id"] for k, r in state["bindings"].items() if k != key}
            if not set(config["identities"]) - used:
                atomic_json(filename, state)
                raise error("No unassigned pane identities remain. Add identities or wait for confirmed stale bindings to be pruned.")
            for _ in range(2 * len(config["identities"])):
                selected = draw_id(paths, config)
                if selected not in used:
                    break
            else:
                raise error("Could not allocate a free pane identity.")
            title_prefix = saved.get("title_prefix") if saved else None
            saved = {"daemon": daemon, "pane_id": pane, "id": selected}
            if isinstance(title_prefix, str):
                saved["title_prefix"] = title_prefix
            state["bindings"][key] = saved
        saved.pop("missing_since", None)
        atomic_json(filename, state)
        return saved["id"]


def prefixed_title(title, prefix, previous=None):
    """Strip only the exact prefix we own, never a guessed human-name pattern."""
    if previous and title.startswith(previous):
        title = title[len(previous):]
    return prefix + title


def update_title(paths, name, source, *, private_dir, lock, atomic_json, run=None):
    """Best-effort custom name, with ownership stored alongside the pane binding."""
    identity = owner()
    if identity is None or "pane_title" not in source:
        return
    run = subprocess.run if run is None else run
    key, _, pane = identity
    directory = private_dir(paths.state)
    filename = directory / "pane-identities.json"
    try:
        with lock(directory / "pane-identities.lock"):
            state = json.loads(filename.read_text(encoding="utf-8"))
            saved = state["bindings"][key]
            # Re-read under the lock so concurrent context calls cannot stack prefixes.
            result = run(["tuios", "get-window", pane, "-s", os.environ["TUIOS_SESSION"], "--json"],
                         capture_output=True, encoding="utf-8", timeout=2, check=True)
            window = json.loads(result.stdout)
            if window.get("success") is not True or window.get("id") != pane:
                raise ValueError("Cannot verify the caller pane's current name.")
            title = window.get("display_name")
            if not isinstance(title, str):
                raise ValueError("Pane has no readable display name.")
            prefix = f"[{name}] "
            previous = saved.get("title_prefix")
            updated = prefixed_title(title, prefix, previous if isinstance(previous, str) else None)
            if updated != title:
                run(["tuios", "set-window", "-s", os.environ["TUIOS_SESSION"], "-w", pane, "--name", updated],
                    capture_output=True, encoding="utf-8", timeout=2, check=True)
            saved["title_prefix"] = prefix
            atomic_json(filename, state)
    except subprocess.SubprocessError:
        raise ValueError("Pane naming failed or was not permitted; no retry attempted.") from None
