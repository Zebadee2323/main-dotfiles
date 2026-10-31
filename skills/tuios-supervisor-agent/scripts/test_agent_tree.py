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

    def test_compact_graph_keeps_all_nodes_in_three_rows_each(self):
        self.registry["nodes"].append({
            "id": "second-worker", "parent": "worktree:slug", "kind": "worker",
            "label": "悪い名前", "session": "slug", "pane": "second-worker",
        })
        nodes, _, _ = tree.render_nodes(self.registry, {})
        lines = tree.graph_lines(nodes)
        self.assertEqual(len(lines), 3 * len(self.registry["nodes"]))
        self.assertIn("├──┌─Jaden", tree.box_graph(nodes))
        self.assertIn("│    └──┌─Tess", tree.box_graph(nodes))
        for index, node in enumerate(self.registry["nodes"]):
            box = lines[index * 3:index * 3 + 3]
            self.assertIn(node["label"], box[0]["box"])
            for line in box:
                # Wide labels must not move the right border.
                self.assertEqual(tree.fit_label(line["box"], 36), line["box"])

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


class MonitorFailureTests(unittest.TestCase):
    def test_cli_failure_preserves_both_diagnostic_channels(self):
        import subprocess
        result = subprocess.CompletedProcess([], 1, '{"error":{"code":"forbidden"}}', 'wrong owner')
        with patch.object(tree.subprocess, 'run', return_value=result):
            with self.assertRaises(tree.TuiosError) as raised:
                tree.call_raw('tml-patch', {})
        self.assertEqual(raised.exception.code, 'forbidden')
        self.assertIn('wrong owner', str(raised.exception))
        self.assertIn('forbidden', str(raised.exception))

    def test_transient_patch_replays_all_batches(self):
        ops = [{"op": "replace", "path": "/x", "value": "界" * 10000} for _ in range(3)]
        with patch.object(tree, 'call_raw', side_effect=[{}, tree.TuiosError('transport'), {}, {}, {}]) as send, \
                patch.object(tree.time, 'sleep'):
            tree.patch_surface('surface', ops)
        self.assertEqual(send.call_count, 5)
        self.assertEqual(send.call_args_list[0], send.call_args_list[2])

    def test_denied_patch_is_never_retried(self):
        with patch.object(tree, 'call', side_effect=tree.TuiosError('denied', 'forbidden')) as send:
            with self.assertRaises(tree.TuiosError):
                tree.patch_surface('surface', [])
        self.assertEqual(send.call_count, 1)

    def test_patch_retry_budget_is_bounded(self):
        with patch.object(tree, 'call', side_effect=tree.TuiosError('transport')) as send, \
                patch.object(tree.time, 'sleep'):
            with self.assertRaises(tree.TuiosError):
                tree.patch_surface('surface', [])
        self.assertEqual(send.call_count, 4)

    def test_resolved_ownership_must_match_registry(self):
        registry = {'supervisor_pane': 'expected', 'session': 'project'}
        for actual in [{'pane': False}, {'pane': True, 'window': 'other', 'session': 'project'},
                       {'pane': True, 'window': 'expected', 'session': 'other'}]:
            with patch.object(tree, 'call', return_value=actual):
                with self.assertRaisesRegex(RuntimeError, 'ownership mismatch'):
                    tree.verify_owner(registry)

    def test_start_detaches_before_open_and_waits_for_proof(self):
        self.exercise_start('ok')

    def test_start_reports_detached_ownership_failure(self):
        self.exercise_start('wrong-owner')

    def test_start_reports_first_patch_denial(self):
        self.exercise_start('denied')

    def exercise_start(self, mode):
        """Real double fork/CLI lifecycle against an isolated fake daemon CLI."""
        import subprocess
        import sys
        import time
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = root / 'tree.json'
            registry.write_text(json.dumps({'version': 1, 'session': 'project',
                'supervisor_pane': 'supervisor-id', 'nodes': [
                    {'id': 'supervisor-id', 'kind': 'supervisor', 'label': 'Supervisor',
                     'session': 'project', 'pane': 'supervisor-id'}]}))
            fake = root / 'tuios'
            fake.write_text('''#!PYTHON
import json, os, pathlib, subprocess, sys, time
root = pathlib.Path(os.environ['FAKE_ROOT'])
mode = os.environ['FAKE_MODE']
if sys.argv[1] == 'subscribe':
    print('{"type":"subscribed"}', flush=True)
    time.sleep(30)
    sys.exit()
verb = sys.argv[2]
params = json.loads(sys.argv[3])
# Each CLI must be invoked by the detached monitor, not start.
parent = os.getppid()
with (root / 'calls').open('a') as out:
    out.write(json.dumps({'verb': verb, 'parent': int(parent), 'params': params}) + '\\n')
if verb == 'pane-grants':
    value = {'pane': True, 'session': 'project', 'window': 'other' if mode == 'wrong-owner' else 'supervisor-id'}
elif verb == 'list-windows':
    value = {'windows': [{'window_id': 'supervisor-id', 'workspace': 1}]}
elif verb == 'list-agents':
    value = {'agents': []}
elif verb == 'tml-open':
    value = {'surface': 'test-surface', 'window_id': 'tree-pane'}
elif verb == 'tml-patch':
    if mode == 'denied':
        print('{"error":{"code":"forbidden","message":"wrong owner"}}')
        sys.exit(1)
    value = {'version': 1}
else:
    raise RuntimeError(verb)
print(json.dumps(value))
'''.replace('PYTHON', sys.executable, 1))
            fake.chmod(0o755)
            env = dict(os.environ, PATH=str(root) + os.pathsep + os.environ['PATH'],
                       TUIOS_SESSION='project', TUIOS_PANE_ID='supervisor-id',
                       FAKE_ROOT=str(root), FAKE_MODE=mode)
            runtime = registry.with_suffix('.runtime.json')
            try:
                starter = subprocess.Popen([sys.executable, str(Path(tree.__file__).resolve()),
                    'start', '--registry', str(registry)], env=env, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, text=True)
                stdout, stderr = starter.communicate(timeout=15)
                result = subprocess.CompletedProcess(starter.args, starter.returncode, stdout, stderr)
                self.assertTrue((root / 'calls').exists(), result.stderr + registry.with_suffix('.log').read_text())
                calls = [json.loads(line) for line in (root / 'calls').read_text().splitlines()]
                info = json.loads(runtime.read_text())
                if mode == 'ok':
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(json.loads(result.stdout)['monitor'], 'live')
                    self.assertTrue(any(c['verb'] == 'tml-patch' for c in calls))
                    self.assertEqual(info['monitor'], 'live')
                    self.assertTrue(all(c['parent'] == info['pid'] and c['parent'] != starter.pid for c in calls))
                else:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertTrue(info['monitor'].startswith('error'))
                    self.assertNotIn('"monitor": "live"', result.stdout)
                    if mode == 'wrong-owner':
                        self.assertEqual([c['verb'] for c in calls], ['pane-grants'])
                    else:
                        self.assertEqual(sum(c['verb'] == 'tml-patch' for c in calls), 1)
                        self.assertIn('forbidden', registry.with_suffix('.log').read_text())
            finally:
                registry.with_suffix('.stop').touch()
                deadline = time.monotonic() + 5
                while runtime.exists() and time.monotonic() < deadline:
                    if json.loads(runtime.read_text())['monitor'] != 'live':
                        break
                    time.sleep(0.05)


if __name__ == "__main__":
    unittest.main()
