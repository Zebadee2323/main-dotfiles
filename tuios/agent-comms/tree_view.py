"""Named, identity-owned display trees and a compact, silent renderer."""
from __future__ import annotations

from collections import Counter
import re
import json

import conversation as c


def presentation(state):
    state = state or {}
    return {"trees": state.get("display_trees", {}),
            "roles": {b["name"].lower(): b.get("agent_role", "") for b in state.get("bindings", {}).values()}}


def text(value, what, limit=128):
    if not isinstance(value, str) or not value or len(value) > limit or c.clean(value) != value:
        raise ValueError(f"{what} must be printable text, 1-{limit} characters.")
    return value


def validate_nodes(nodes, state):
    if not isinstance(nodes, list) or not 1 <= len(nodes) <= 256:
        raise ValueError("Supply an array of 1-256 nodes.")
    result, ids, agents = [], set(), set()
    bindings = {b["name"].lower(): b["name"] for b in state.get("bindings", {}).values()}
    for node in nodes:
        if not isinstance(node, dict) or set(node) - {"id", "parent", "label", "agent", "info"}:
            raise ValueError("Each node accepts id, parent, label, optional agent and info.")
        ident = text(node.get("id"), "Node ID", 64)
        if ident in ids:
            raise ValueError("Node IDs must be unique.")
        ids.add(ident)
        item = {"id": ident}
        if node.get("parent") is not None:
            item["parent"] = text(node["parent"], "Parent ID", 64)
        if "agent" in node:
            name = text(node["agent"], "Agent name", 64).lower()
            if name not in bindings or name in agents:
                raise ValueError("Agent nodes need a current, unique agent identity.")
            agents.add(name)
            item["agent"] = bindings[name]
        item["label"] = text(node.get("label", item.get("agent", ident)), "Label")
        if node.get("info"):
            item["info"] = text(node["info"], "Node info", 160)
        elif "info" in node and node["info"] != "":
            raise ValueError("Node info must be printable text.")
        result.append(item)
    by_id = {n["id"]: n for n in result}
    for node in result:
        seen, cursor = set(), node
        while cursor.get("parent") is not None:
            if cursor["id"] in seen:
                raise ValueError("Tree must not contain cycles.")
            seen.add(cursor["id"])
            if cursor["parent"] not in by_id:
                raise ValueError("Unknown parent ID.")
            cursor = by_id[cursor["parent"]]
            if len(seen) > 32:
                raise ValueError("Tree depth must not exceed 32.")
    return result


def change(state, binding, request):
    if request["command"] == "role":
        value = request.get("value")
        if value != "":
            text(value, "Role")
        binding["agent_role"] = value
        return {"name": binding["name"], "role": value}
    name = text(request.get("tree_name"), "Tree name", 64)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name):
        raise ValueError("Tree names use letters, digits, dots, underscores, and hyphens.")
    trees = state.setdefault("display_trees", {})
    current = trees.get(name)
    action = request.get("action")
    if action == "show":
        return current
    if current and current["owner"] != binding["name"]:
        raise ValueError("Only the tree's creating identity may change or delete it.")
    if action == "delete":
        trees.pop(name, None)
        return None
    if action != "set":
        raise ValueError("Unknown tree action.")
    nodes = validate_nodes(request.get("nodes", (current or {}).get("nodes")), state)
    title = request.get("title", (current or {}).get("title", ""))
    if title != "":
        text(title, "Title", 160)
    if not current and len(trees) >= 32:
        raise ValueError("At most 32 named trees are supported; delete an unused tree first.")
    trees[name] = {"owner": binding["name"], "nodes": nodes, "title": title}
    # Keep every watcher snapshot comfortably within the protocol frame budget.
    if len(json.dumps(presentation(state)).encode()) > 24000:
        if current is None:
            trees.pop(name)
        else:
            trees[name] = current
        raise ValueError("Tree display metadata exceeds the shared size limit; shorten or remove nodes.")
    return trees[name]


class TreeView:
    def __init__(self, name, *, color=True):
        self.name, self.color = name, color
        self.data, self.queued, self.message = {}, [], None
        self.offline, self.notice = True, None

    def metadata(self, data, queued):
        self.data, self.queued = data, queued

    def update(self, message, now, *, offline=False):
        self.offline = offline
        self.message = None if offline else message

    def render(self, width, height, now):
        canvas = c.Canvas(width, height)
        tree = self.data.get("trees", {}).get(self.name)
        status = (self.notice or "offline") if self.offline else ("live" if tree else "waiting for tree")
        title = (tree or {}).get("title") or self.name
        canvas.put(0, 0, c.fit(f"{title} · {status}", width), "1")
        if not tree or height < 2:
            return canvas.lines(self.color)
        nodes = [dict(n) for n in tree["nodes"]]
        message = self.message or {}
        speaker = str(message.get("name", "")).lower()
        recipient = message.get("recipient") or {}
        target = str(recipient.get("name", "")).lower()
        names = {n.get("agent", "").lower() for n in nodes}
        for name, kind in ((message.get("name"), "agent"), (recipient.get("name"), recipient.get("kind"))):
            if name and name.lower() not in names:
                nodes.append({"id": "external:" + name, "label": name, "agent": name if kind == "agent" else "", "external": name.lower()})
                names.add(name.lower())
        counts = Counter(str(m.get("name", "")).lower() for m in self.queued)
        rows = []
        def visit(parent, prefix=""):
            children = [n for n in nodes if n.get("parent") == parent]
            for i, node in enumerate(children):
                last = i == len(children) - 1
                rows.append((node, prefix + ("└─ " if last else "├─ ")))
                if node.get("info"):
                    rows.append(({**node, "_info": True}, prefix + ("   " if last else "│  ")))
                visit(node["id"], prefix + ("   " if last else "│  "))
        visit(None)
        active = {n["id"]: i for i, (n, _) in enumerate(rows)
                  if not n.get("_info") and (n.get("agent", "").lower() or n.get("external")) in {n for n in (speaker, target) if n}}
        source_row = next((i for i, (n, _) in enumerate(rows) if not n.get("_info") and n.get("agent", "").lower() == speaker and speaker), None)
        target_row = next((i for i, (n, _) in enumerate(rows) if not n.get("_info") and (n.get("agent", "").lower() or n.get("external")) == target and target), None)
        capacity = height - 1
        overflow = len(rows) > capacity
        content_rows = capacity - 1 if overflow and capacity > 1 else capacity
        # Center the speaker within the rows available after the overflow footer.
        start = max(0, (source_row or 0) - content_rows // 2) if overflow else 0
        start = min(start, max(0, len(rows) - content_rows))
        visible = rows[start:start + content_rows]
        for offset, (node, branch) in enumerate(visible):
            i = start + offset
            name = node.get("agent", "").lower()
            role = self.data.get("roles", {}).get(name, "")
            label = node.get("agent") or node["label"]
            label += " · " + role if role else ""
            if counts[name] and not self.offline and not node.get("_info"):
                label += f" [{counts[name]} queued]"
            playing = name == speaker and bool(speaker) and not node.get("_info")
            marker = "▶ " if playing and message.get("phase") == "displaying" else "◌ " if playing else ""
            rail = "  "
            if source_row is not None and target_row is not None and source_row != target_row:
                lo, hi = sorted((source_row, target_row))
                rail = "┌─" if i == lo else "└─" if i == hi else "│ " if lo < i < hi else "  "
                if i == target_row:
                    rail = "└▶" if i == hi else "┌▶"
            style = "1;36" if playing else "1;33" if node["id"] in active else ""
            if node.get("_info"):
                label, marker, style = node["info"], "", "2"
            # Canvas sanitizes prose whitespace; draw connectors at explicit cell
            # positions so indentation survives instead of collapsing into a list.
            prefix = rail + branch
            for x, char in enumerate(prefix):
                if char != " ":
                    canvas.put(x, offset + 1, char, style)
            x = len(prefix)
            canvas.put(x, offset + 1, c.fit(marker + label, max(0, width - x)), style)
        if overflow and capacity > 1:
            canvas.put(0, height - 1, c.fit(f"… {len(rows) - len(visible)} lines outside view", width), "2")
        return canvas.lines(self.color)
