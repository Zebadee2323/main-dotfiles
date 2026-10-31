"""Focused tests for registry boundaries, stable topology, and graph rendering."""

import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import agent_tree as tree


class AgentTreeTests(unittest.TestCase):
    def setUp(self):
        self.registry = {"version": 1, "session": "project", "supervisor_pane": "supervisor-id", "nodes": [
            {"id": "supervisor-id", "kind": "supervisor", "label": "Justin", "session": "project", "pane": "supervisor-id"},
            {"id": "worktree:slug", "parent": "supervisor-id", "kind": "worktree", "label": "slug", "session": "slug"},
            {"id": "worker-id", "parent": "worktree:slug", "kind": "worker", "label": "Jaden", "session": "slug", "pane": "worker-id"},
            {"id": "reviewer-id", "parent": "worker-id", "kind": "reviewer", "label": "Tess", "session": "slug", "pane": "reviewer-id"},
        ]}

    def load(self, value):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "registry.json"
            path.write_text(json.dumps(value))
            with patch.dict(os.environ, TUIOS_PANE_ID="supervisor-id", TUIOS_SESSION="project"):
                return tree.load_registry(path)

    def test_reject_wrong_owner_duplicate_and_cycle(self):
        for change in [
            lambda r: r.update(supervisor_pane="another-pane"),
            lambda r: r["nodes"].append(copy.deepcopy(r["nodes"][2])),
            lambda r: r["nodes"][1].update(parent="reviewer-id"),
        ]:
            registry = copy.deepcopy(self.registry)
            change(registry)
            with self.assertRaises(ValueError):
                self.load(registry)

    def test_session_and_pane_binding_excludes_name_collision(self):
        agents = {("unrelated", "worker-id"): {"state": "done"}}
        nodes, rows, _ = tree.render_nodes(self.registry, agents)
        self.assertEqual(rows["worker-id"]["detail"], "worker · missing")
        self.assertEqual(len(nodes), 1)

    def test_status_changes_keep_paths_and_box_geometry(self):
        agents = {("slug", "worker-id"): {"state": "idle"}}
        before, _, paths = tree.render_nodes(self.registry, agents)
        agents[("slug", "worker-id")]["state"] = "needs_input"
        after, _, new_paths = tree.render_nodes(self.registry, agents)
        self.assertEqual(paths, new_paths)
        before_graph, after_graph = tree.box_graph(before), tree.box_graph(after)
        self.assertNotEqual(before_graph, after_graph)
        self.assertEqual([len(line) for line in before_graph.splitlines()],
                         [len(line) for line in after_graph.splitlines()])
        self.assertIn("reviewer · missing", after_graph)
        self.assertIn("└──┌", after_graph)

    def test_labels_cannot_inject_terminal_controls_or_graph_lines(self):
        value = tree.fit_label("悪い\nname\x1b[31m")
        self.assertNotIn("\n", value)
        self.assertNotIn("\x1b", value)
        self.assertEqual(sum(0 if tree.unicodedata.combining(c) else 2 if tree.unicodedata.east_asian_width(c) in "WF" else 1 for c in value), 32)

    def test_patch_batches_respect_wire_budget(self):
        ops = [{"op": "replace", "path": "/x", "value": "界" * 10000} for _ in range(3)]
        with patch.object(tree, "call_raw", return_value={}) as send:
            tree.call("tml-patch", surface="test-surface", ops=ops)
        self.assertEqual(send.call_count, 3)
        self.assertTrue(all(len(json.dumps(call.args[1]["ops"], ensure_ascii=False).encode()) < 60000 for call in send.call_args_list))

    def test_graph_uses_bound_text_not_unrendered_mermaid(self):
        nodes, _, _ = tree.render_nodes(self.load(self.registry), {})
        doc = tree.document(nodes)
        graph = doc["children"][0]["children"][2]["children"][0]
        self.assertEqual(graph["type"], "list")
        self.assertEqual(graph["items"], "state.lines")
        self.assertEqual(set(row["role"] for row in doc["state"]["lines"]) - {"dim"}, set(tree.ROLES.values()))
        self.assertEqual(len(set(tree.ROLES.values())), 4)
        self.assertIn("Justin", tree.box_graph(nodes))


if __name__ == "__main__":
    unittest.main()
