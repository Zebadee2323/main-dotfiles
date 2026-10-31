"""Durable pane/shell bindings and conservative, owner-scoped garbage collection.

Bindings live in the allocation store (allocation.py); this module derives the
verified owner key, prunes confirmed-absent owners, and maintains pane titles.
Pruned or cleared bindings never return their names to the name bag.
"""
import hashlib
import json
import os
import re
import subprocess
import time

import allocation
import tuios_context
import shell_identity

GRACE_SECONDS = 7 * 24 * 3600
SCAN_INTERVAL = 3600

MissingIdentityError = allocation.MissingBinding


def owner(env=None):
    explicit_env = env is not None
    env = os.environ if env is None else env
    if env.get("TUIOS_ENV") != "1":
        return shell_identity.owner(env.get(shell_identity.ENV_KEY) if explicit_env else None)
    pane = env.get("TUIOS_PANE_ID") or env.get("TUIOS_WINDOW_ID")
    if not pane:
        raise ValueError("tuios pane ID is missing")
    # Socket path, never PID/token: stable across daemon and machine restarts.
    daemon = [env.get("TUIOS_HOST", "local"), env.get("TUIOS_SOCKET") or env.get("TUIOS_SERVER", "default")]
    daemon_key = hashlib.sha256(json.dumps(daemon).encode()).hexdigest()
    key = hashlib.sha256(json.dumps([daemon_key, pane]).encode()).hexdigest()
    return key, daemon_key, pane


def inventory(run=subprocess.run, env=None):
    """Only a complete successful listing is evidence that a pane is gone."""
    try:
        result = run([tuios_context.executable(env), "list-windows", "--all", "--json"], capture_output=True,
                     encoding="utf-8", timeout=2, check=True, **({"env": env} if env is not None else {}))
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
            allocation.retire_prefix(state, state["bindings"].pop(key))


def store(paths, *, lock, atomic_json, private_dir=None):
    return allocation.Store(paths.state, lock=lock, atomic_json=atomic_json, private_dir=private_dir)


def clear_all(paths, *, private_dir, lock, atomic_json, error):
    """Administrative clear of every owner binding. Bags, generation, and epoch stay."""
    def operation(state):
        if state is None:
            return {"cleared": 0}
        count = len(state["bindings"])
        for record in state["bindings"].values():
            allocation.retire_prefix(state, record)
        state["bindings"], state["scanned"] = {}, {}
        return {"cleared": count}
    try:
        return store(paths, lock=lock, atomic_json=atomic_json, private_dir=private_dir).update(operation)
    except allocation.AllocationError as exc:
        raise error(str(exc)) from None


def lookup(paths, env=None):
    """Read-only (epoch, binding) for the verified owner; never creates or locks files."""
    key = owner(env)[0]
    return allocation.Store(paths.state, lock=None, atomic_json=None).binding(key)


def resolve(paths, config, *, private_dir, lock, atomic_json, error, clear=False, env=None, rng=None):
    """Return the owner's binding (allocating on first use) as (epoch, record)."""
    key, daemon, pane = owner(env)

    def live_owners():
        if daemon.startswith("shell:"):
            return shell_identity.inventory()
        return inventory(env=env) if env is not None else inventory()

    def operation(state):
        if clear:
            saved = state["bindings"].pop(key, None) if state is not None else None
            if saved is not None:
                allocation.retire_prefix(state, saved)
            return {"cleared": saved is not None, "name": saved["name"] if saved else None}
        now = time.time()
        if now - state["scanned"].get(daemon, 0) >= SCAN_INTERVAL:
            prune(state, daemon, pane, live_owners(), now)
            # Throttle failures too; never retry a forbidden query via another route.
            state["scanned"][daemon] = now
        saved = state["bindings"].get(key)
        if saved is None:
            saved = allocation.allocate(state, config, target.rng, key=key, daemon=daemon, pane=pane)
        saved.pop("missing_since", None)
        return state["epoch"], dict(saved)

    target = store(paths, lock=lock, atomic_json=atomic_json, private_dir=private_dir)
    if rng is not None:
        target.rng = rng
    try:
        return target.update(operation, config=None if clear else config)
    except allocation.AllocationError as exc:
        raise error(str(exc)) from None


def title_names(config, retired=()):
    """Recognize generated-name title prefixes without touching unrelated labels."""
    bases = "|".join(sorted((re.escape(name) for name, _ in config.get("names", ())), key=len, reverse=True))
    pattern = re.compile(rf"\[(?:{bases})(?:[2-9]|[1-9][0-9]+)?\] ") if bases else None
    return pattern, tuple(retired)


def prefixed_title(title, prefix, previous=None, names=(), pattern=None):
    """Normalize known agent prefixes, preserving unrelated bracketed labels."""
    prefixes = {prefix, *(names or ())}
    if previous:
        prefixes.add(previous)
    while True:
        matched = next((p for p in prefixes if p and title.startswith(p)), None)
        if matched is None and pattern is not None:
            found = pattern.match(title)
            matched = found.group(0) if found else None
        if matched is None:
            break
        title = title[len(matched):]
    return prefix + title


def update_title(paths, name, source, *, config, private_dir, lock, atomic_json, run=None, env=None):
    """Best-effort name prefix over the pane's live terminal title."""
    env = os.environ if env is None else env
    if env.get("TUIOS_ENV") != "1" or "pane_title" not in source:
        return
    key, _, pane = owner(env)
    run = subprocess.run if run is None else run

    def operation(state):
        saved = (state or {}).get("bindings", {}).get(key)
        if saved is None or saved["name"] != name:
            raise ValueError("Pane binding changed before its name could be shown.")
        # Re-read under the lock so concurrent context calls cannot stack prefixes.
        result = run([tuios_context.executable(env), "get-window", pane, "-s", env["TUIOS_SESSION"], "--json"],
                     capture_output=True, encoding="utf-8", timeout=2, check=True, env=env)
        window = json.loads(result.stdout)
        identifiers = [window[field] for field in ("id", "window_id") if field in window]
        if window.get("success") is not True or not identifiers or any(value != pane for value in identifiers):
            raise ValueError("Cannot verify the caller pane's current name.")
        title = window.get("title")
        if not isinstance(title, str):
            raise ValueError("Pane has no readable terminal title.")
        custom = window.get("custom_name", "")
        if not isinstance(custom, str):
            raise ValueError("Pane has no readable custom name.")
        prefix = f"[{name}] "
        previous = saved.get("title_prefix")
        pattern, retired = title_names(config, state.get("retired_title_prefixes", ()))
        updated = prefixed_title(title, prefix, previous if isinstance(previous, str) else None, retired, pattern)
        if updated != custom:
            run([tuios_context.executable(env), "set-window", "-s", env["TUIOS_SESSION"], "-w", pane, "--name", updated],
                capture_output=True, encoding="utf-8", timeout=2, check=True, env=env)
        saved["title_prefix"] = prefix

    try:
        store(paths, lock=lock, atomic_json=atomic_json, private_dir=private_dir).update(operation)
    except subprocess.SubprocessError:
        raise ValueError("Pane naming failed or was not permitted; no retry attempted.") from None
