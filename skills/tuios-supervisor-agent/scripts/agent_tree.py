#!/usr/bin/env python3
"""Own a TML agent tree; topology comes from a registry, status from tuios events."""

import argparse
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import unicodedata


STATES = {"working", "needs_input", "idle", "done", "errored", "unknown", "none"}
EVENTS = "agent-state,window-closed,window-exit,tml-event"
ROLES = {"supervisor": "accent", "worktree": "info", "worker": "success", "reviewer": "warning"}


def atomic_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def call(verb, **params):
    if verb == "tml-patch":
        # Topology and graph can exceed the patch limit together; bound each call.
        batch = []
        for operation in params["ops"]:
            if len(json.dumps([operation], ensure_ascii=False).encode()) > 60000:
                raise ValueError("single TML patch exceeds size budget; use fewer/shorter nodes")
            if batch and len(json.dumps(batch + [operation], ensure_ascii=False).encode()) > 60000:
                call_patch(params["surface"], batch)
                batch = []
            batch.append(operation)
        return call_patch(params["surface"], batch)
    return call_raw(verb, params)


def call_patch(surface, ops):
    return call_raw("tml-patch", {"surface": surface, "ops": ops})


class TuiosError(RuntimeError):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


def call_raw(verb, params):
    result = subprocess.run(
        ["tuios", "call", verb, json.dumps(params, ensure_ascii=False)], capture_output=True,
        text=True, timeout=15,
    )
    try:
        value = json.loads(result.stdout)
    except ValueError:
        value = {}
    error = value.get("error") if isinstance(value, dict) else None
    if result.returncode or error:
        code = error.get("code") if isinstance(error, dict) else None
        # Include both channels: tuios puts verb failures in stdout, transport
        # and CLI diagnostics in stderr. Do not log params (which may be large).
        raise TuiosError(
            f"tuios {verb} failed (exit {result.returncode}): "
            f"stdout={result.stdout.strip()!r}; stderr={result.stderr.strip()!r}", code)
    if not isinstance(value, dict) or not value:
        raise TuiosError(f"tuios {verb} returned invalid/empty JSON: {result.stdout!r}")
    return value


def patch_surface(surface, ops):
    """Retry idempotent replacements, including partially sent split batches."""
    ops = list(ops) + [{"op": "replace", "path": "/updated",
                        "value": datetime.now(timezone.utc).isoformat(timespec="seconds")}]
    for attempt in range(4):
        try:
            return call("tml-patch", surface=surface, ops=ops)
        except (TuiosError, subprocess.TimeoutExpired) as error:
            print(f"Patch attempt {attempt + 1} failed: {error}", file=sys.stderr, flush=True)
            # A denial is final; never retry it through another identity/verb.
            if isinstance(error, TuiosError) and error.code in {
                "forbidden", "not_human", "unknown_pane", "no_surface",
                "invalid_params", "invalid_tml",
            }:
                raise
            if attempt == 3:
                raise
            time.sleep(0.25 * (attempt + 1))


def verify_owner(registry):
    actual = call("pane-grants")
    if (not actual.get("pane") or actual.get("window") != registry["supervisor_pane"]
            or actual.get("session") != registry["session"]):
        raise RuntimeError(
            "detached helper ownership mismatch: expected "
            f"{registry['session']}/{registry['supervisor_pane']}, got "
            f"{actual.get('session')}/{actual.get('window')} via {actual.get('via')}; "
            "no surface opened; use run in a persistent supervisor tool process "
            "or fix tuios caller resolution")


def load_registry(path):
    registry = json.loads(path.read_text())
    if registry.get("version") != 1:
        raise ValueError("registry version must be 1")
    if registry["supervisor_pane"] != os.environ.get("TUIOS_PANE_ID"):
        raise ValueError("run from the actual supervisor pane; never spoof TUIOS_PANE_ID")
    if registry["session"] != os.environ.get("TUIOS_SESSION"):
        raise ValueError("registry session must be the supervisor session")
    nodes = registry["nodes"]
    if not nodes or len(nodes) > 60:
        raise ValueError("registry must contain 1–60 nodes")
    seen, panes = {}, set()
    for node in nodes:
        key = node["id"]
        if not isinstance(key, str) or key in seen:
            raise ValueError("node IDs must be unique strings")
        if not isinstance(node["label"], str) or not node["label"] or len(node["label"]) > 64:
            raise ValueError("use a short nonempty node label (at most 64 characters)")
        parent = node.get("parent")
        if parent is not None and parent not in seen:
            raise ValueError("list parents before children; missing parents/cycles are invalid")
        depth = 0 if parent is None else seen[parent] + 1
        if depth > 8:
            raise ValueError("agent tree depth must not exceed 8")
        seen[key] = depth
        if node["kind"] not in {"supervisor", "worktree", "worker", "reviewer"}:
            raise ValueError("unsupported node kind")
        if node["kind"] != "worktree":
            binding = (node["session"], node["pane"])
            if binding in panes or key != node["pane"]:
                raise ValueError("agent node ID must be its unique full pane ID")
            panes.add(binding)
    roots = [n for n in nodes if n.get("parent") is None]
    if len(roots) != 1 or roots[0]["id"] != registry["supervisor_pane"] or roots[0]["kind"] != "supervisor":
        raise ValueError("the supervisor must be the single root")
    if roots[0]["session"] != registry["session"]:
        raise ValueError("supervisor node session mismatch")
    return registry


def render_nodes(registry, agents):
    """Stable registry order and pane bindings; never infer membership from names."""
    roots, by_id, paths = [], {}, {}
    for node in registry["nodes"]:
        parent = node.get("parent")
        children = roots if parent is None else by_id[parent]["children"]
        path = "/nodes" if parent is None else paths[parent] + "/children"
        path += "/" + str(len(children))
        label = node["label"]
        detail = "worktree"
        if node["kind"] != "worktree":
            agent = agents.get((node["session"], node["pane"]))
            state = "missing" if agent is None else agent.get("state", "unknown")
            if state not in STATES | {"missing"}:
                state = "unknown"
            label += " · " + node["kind"] + " · " + state
            detail = node["kind"] + " · " + state
        row = {"id": node["id"], "label": label, "name": node["label"],
               "detail": detail, "role": ROLES[node["kind"]], "children": []}
        children.append(row)
        by_id[node["id"]], paths[node["id"]] = row, path
    return roots, by_id, paths


def fit_label(value, width=32):
    """Cell-aware padding; labels cannot inject new lines or terminal controls."""
    result, cells = "", 0
    for char in value:
        if unicodedata.category(char).startswith("C") or char in "\r\n\t":
            char = " "
        size = 0 if unicodedata.combining(char) else (2 if unicodedata.east_asian_width(char) in "WF" else 1)
        if cells + size > width:
            break
        result += char
        cells += size
    return result + " " * (width - cells)


def graph_lines(nodes):
    """Three-row boxes with names in their borders and no spacer rows."""
    lines = []

    def emit(prefix, box, role):
        lines.append({"prefix": prefix, "box": box, "role": role})

    def draw(node, prefix="", branch=""):
        continuation = prefix + ("│  " if branch == "├──" else "   " if branch else "")
        role = node["role"]
        title = fit_label(node["name"])
        border = title.rstrip() + "─" * (len(title) - len(title.rstrip()))
        emit(prefix + branch, "┌─" + border + "─┐", role)
        emit(continuation, "│ " + fit_label(node["detail"]) + " │", role)
        emit(continuation, "└─" + ("┬" if node["children"] else "─") + "─" * 32 + "┘", role)
        for index, child in enumerate(node["children"]):
            draw(child, continuation + "  ", "└──" if index == len(node["children"]) - 1 else "├──")

    for root in nodes:
        draw(root)
    return lines


def box_graph(nodes):
    return "\n".join(line["prefix"] + line["box"] for line in graph_lines(nodes))


def document(nodes):
    return {
        "type": "tml", "version": 1, "title": "Agent tree",
        "state": {"nodes": nodes, "lines": graph_lines(nodes), "monitor": "starting; status unverified", "updated": "startup"},
        "children": [{"type": "col", "pad": 0, "gap": 0, "children": [
            {"type": "text", "role": "dim", "children": ["Status at {state.updated}: {state.monitor}"]},
            {"type": "text", "children": [
                {"type": "c", "role": role, "children": [kind + "  "]}
                for kind, role in ROLES.items()
            ]},
            {"type": "scroll", "h": "fill", "children": [
                {"type": "list", "items": "state.lines", "as": "line", "select": "none", "children": [
                    {"type": "text", "wrap": "none", "children": [
                        {"type": "c", "role": "dim", "children": ["{line.prefix}"]},
                        {"type": "c", "role": "{line.role}", "children": ["{line.box}"]},
                    ]},
                ]},
            ]},
        ]}],
    }


def snapshot():
    result = call("list-agents", all=True, all_sessions=True)
    return {(a["session"], a["window_id"]): a for a in result["agents"]}


def subscribe(inbox):
    process = subprocess.Popen(
        ["tuios", "subscribe", "--types", EVENTS], stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, bufsize=1,
    )

    def read():
        try:
            for line in process.stdout:
                inbox.put(json.loads(line))
        except (ValueError, OSError) as error:
            inbox.put({"type": "stream-error", "error": str(error)})
        finally:
            inbox.put({"type": "disconnected"})

    threading.Thread(target=read, daemon=True).start()
    return process


def end_stream(process):
    if process is not None:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def run(path):
    registry = load_registry(path)
    runtime = path.with_suffix(".runtime.json")
    stop = path.with_suffix(".stop")
    # This lock lives for the process lifetime; stale PID files never defeat it.
    with path.with_suffix(".lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        stop.unlink(missing_ok=True)
        verify_owner(registry)
        windows = call("list-windows", session=registry["session"])["windows"]
        owner = [w for w in windows if w["window_id"] == registry["supervisor_pane"]]
        if len(owner) != 1:
            raise ValueError("could not locate exact supervisor pane/workspace")
        workspace = owner[0]["workspace"]
        inbox = queue.Queue()
        stream = subscribe(inbox)  # Subscribe before the initial snapshot.
        surface = None
        reason = "stopped"
        denied = False
        try:
            first = inbox.get(timeout=15)
            if first.get("type") != "subscribed":
                raise RuntimeError("event subscription failed: " + str(first))
            agents = snapshot()
            nodes, rows, paths = render_nodes(registry, agents)
            lines = graph_lines(nodes)
            opened = call("tml-open", session=registry["session"], target="pane",
                          workspace=workspace, name="Agent tree", doc=document(nodes))
            surface = opened["surface"]
            info = {"pid": os.getpid(), "surface": surface, "window_id": opened["window_id"],
                    "owner": registry["supervisor_pane"], "session": registry["session"],
                    "workspace": workspace, "monitor": "starting; status unverified", "subscriber_pid": stream.pid}
            atomic_json(runtime, info)
            # All calls, including open and this proof, run after reparenting.
            patch_surface(surface, [{"op": "replace", "path": "/monitor", "value": "live"}])
            info["monitor"] = "live"
            atomic_json(runtime, info)
            stamp = path.stat().st_mtime_ns
            failures = 0
            while not stop.exists():
                try:
                    event = inbox.get(timeout=0.5)
                except queue.Empty:
                    event = {}
                batch = [event]
                if event:
                    # Coalesce bursts, with at most five canonical refreshes per second.
                    time.sleep(0.2)
                    for _ in range(255):
                        try:
                            batch.append(inbox.get_nowait())
                        except queue.Empty:
                            break
                kinds = {e.get("type") for e in batch}
                if any(e.get("type") == "tml-event" and e.get("tml", {}).get("surface") == surface and e["tml"].get("name") == "close" for e in batch):
                    surface = None  # User dismissal is final; never reopen automatically.
                    reason = "dismissed"
                    break
                if any(e.get("type") == "window-closed" and e.get("window") == registry["supervisor_pane"] for e in batch):
                    reason = "owner closed"
                    break
                changed = path.stat().st_mtime_ns != stamp
                if changed:
                    updated = load_registry(path)  # Atomic registry replacements only.
                    if (updated["session"], updated["supervisor_pane"]) != (registry["session"], registry["supervisor_pane"]):
                        raise ValueError("cannot change dashboard ownership")
                    registry, stamp = updated, path.stat().st_mtime_ns
                bindings = {(n["session"], n["pane"]) for n in registry["nodes"] if "pane" in n}
                relevant = any((e.get("session"), e.get("window")) in bindings for e in batch)
                if kinds & {"disconnected", "stream-error"}:
                    failures += 1
                    if failures > 3:
                        raise RuntimeError("event stream unavailable after three reconnects")
                    patch_surface(surface, [{"op": "replace", "path": "/monitor", "value": "reconnecting; status stale"}])
                    info["monitor"] = "reconnecting; status stale"
                    atomic_json(runtime, info)
                    end_stream(stream)
                    time.sleep(min(failures, 3))
                    inbox = queue.Queue()
                    stream = subscribe(inbox)
                    info["subscriber_pid"] = stream.pid
                    atomic_json(runtime, info)
                    continue
                resync = bool(kinds & {"subscribed", "gap"}) or relevant
                if not changed and not resync:
                    continue
                # Read canonical state, not arbitrary event payloads; missing != done.
                agents = snapshot()
                new_nodes, new_rows, new_paths = render_nodes(registry, agents)
                ops = []
                if changed:
                    ops.append({"op": "replace", "path": "/nodes", "value": new_nodes})
                    ops.append({"op": "replace", "path": "/lines", "value": graph_lines(new_nodes)})
                else:
                    for key, row in new_rows.items():
                        if row["label"] != rows[key]["label"]:
                            ops.append({"op": "replace", "path": paths[key] + "/label", "value": row["label"]})
                            ops.append({"op": "replace", "path": paths[key] + "/detail", "value": row["detail"]})
                    for index, line in enumerate(graph_lines(new_nodes)):
                        if line["box"] != lines[index]["box"]:
                            ops.append({"op": "replace", "path": "/lines/" + str(index) + "/box", "value": line["box"]})
                if "subscribed" in kinds:
                    ops.append({"op": "replace", "path": "/monitor", "value": "live"})
                if ops:
                    patch_surface(surface, ops)
                if "subscribed" in kinds:
                    info["monitor"] = "live"
                    atomic_json(runtime, info)
                rows, paths = new_rows, new_paths
                lines = graph_lines(new_nodes)
        except Exception as error:
            reason = "error; status stale (see log)"
            denied = isinstance(error, TuiosError) and error.code in {"forbidden", "not_human"}
            if runtime.exists():
                info["error"] = str(error)
                atomic_json(runtime, info)
            raise
        finally:
            end_stream(stream)
            if surface and not denied:
                try:
                    patch_surface(surface, [{"op": "replace", "path": "/monitor", "value": reason}])
                except Exception as error:
                    print("Final monitor update failed:", error, file=sys.stderr)
            if runtime.exists():
                info = json.loads(runtime.read_text())
                info["monitor"] = reason
                atomic_json(runtime, info)
            stop.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["run", "start", "stop"])
    parser.add_argument("--detach", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--registry", required=True, type=Path)
    args = parser.parse_args()
    path = args.registry.resolve()
    load_registry(path)
    if args.action == "run":
        if args.detach:
            # The short-lived launcher exits BEFORE the monitor makes any tuios
            # calls. Otherwise open can resolve via its ancestry and later
            # patches via a different owner once start has returned.
            parent = os.getpid()
            if os.fork():
                return
            while os.getppid() == parent:
                time.sleep(0.01)
        try:
            run(path)
        except BlockingIOError:
            raise  # Another helper owns the lock; never overwrite its runtime.
        except Exception as error:
            runtime = path.with_suffix(".runtime.json")
            if not runtime.exists():
                atomic_json(runtime, {"pid": os.getpid(), "monitor": "error; status stale (see log)",
                                      "error": str(error)})
            raise
    elif args.action == "stop":
        path.with_suffix(".stop").touch()
    else:
        # A second start fails rather than opening a duplicate surface.
        with path.with_suffix(".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            runtime = path.with_suffix(".runtime.json")
            if runtime.exists():
                raise ValueError("runtime exists; inspect/close the old surface before restarting")
        with path.with_suffix(".log").open("a") as log:
            process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "run", "--detach", "--registry", str(path)],
                                       stdout=log, stderr=log, start_new_session=True)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if runtime.exists():
                info = json.loads(runtime.read_text())
                if info["monitor"] == "live":
                    print(json.dumps(info, ensure_ascii=False, indent=2))
                    return
                if info["monitor"].startswith("error"):
                    raise RuntimeError("monitor failed: " + info.get("error", "see log")
                                       + "; inspect " + str(path.with_suffix(".log")))
            if process.poll() not in (None, 0):
                raise RuntimeError("monitor launcher failed; inspect " + str(path.with_suffix(".log")))
            time.sleep(0.1)
        path.with_suffix(".stop").touch()
        raise RuntimeError("monitor startup timed out; stop requested; inspect log/process before retrying")


if __name__ == "__main__":
    main()
