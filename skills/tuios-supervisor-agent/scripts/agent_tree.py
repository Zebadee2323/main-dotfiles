#!/usr/bin/env python3
"""Own a TML agent tree; topology comes from a registry, status from tuios events."""

import argparse
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


def call_raw(verb, params):
    result = subprocess.run(
        ["tuios", "call", verb, json.dumps(params, ensure_ascii=False)], capture_output=True,
        text=True, timeout=15, check=True,
    )
    value = json.loads(result.stdout)
    if "error" in value:
        raise RuntimeError(str(value["error"]))
    return value


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
        "state": {"nodes": nodes, "lines": graph_lines(nodes), "monitor": "live"},
        "children": [{"type": "col", "pad": 0, "gap": 0, "children": [
            {"type": "text", "role": "dim", "children": ["Status: {state.monitor}"]},
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
        windows = call("list-windows", session=registry["session"])["windows"]
        owner = [w for w in windows if w["window_id"] == registry["supervisor_pane"]]
        if len(owner) != 1:
            raise ValueError("could not locate exact supervisor pane/workspace")
        workspace = owner[0]["workspace"]
        inbox = queue.Queue()
        stream = subscribe(inbox)  # Subscribe before the initial snapshot.
        surface = None
        reason = "stopped"
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
                    "workspace": workspace, "monitor": "live", "subscriber_pid": stream.pid}
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
                    call("tml-patch", surface=surface, ops=[{"op": "replace", "path": "/monitor", "value": "reconnecting; status stale"}])
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
                    info["monitor"] = "live"
                    atomic_json(runtime, info)
                if ops:
                    call("tml-patch", surface=surface, ops=ops)
                rows, paths = new_rows, new_paths
                lines = graph_lines(new_nodes)
        except Exception:
            reason = "error; status stale (see log)"
            raise
        finally:
            end_stream(stream)
            if surface:
                try:
                    call("tml-patch", surface=surface, ops=[{"op": "replace", "path": "/monitor", "value": reason}])
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
    parser.add_argument("--registry", required=True, type=Path)
    args = parser.parse_args()
    path = args.registry.resolve()
    load_registry(path)
    if args.action == "run":
        run(path)
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
            process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "run", "--registry", str(path)],
                                       stdout=log, stderr=log, start_new_session=True)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if runtime.exists():
                print(runtime.read_text(), end="")
                return
            if process.poll() is not None:
                raise RuntimeError("monitor failed; inspect " + str(path.with_suffix(".log")))
            time.sleep(0.1)
        raise RuntimeError("monitor startup pending; inspect log/process before retrying")


if __name__ == "__main__":
    main()
