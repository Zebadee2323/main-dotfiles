import unittest

import conversation
import tree_view as t


class TreeTests(unittest.TestCase):
    def setUp(self):
        self.dennis = {"name": "Dennis"}
        self.debbie = {"name": "Debbie"}
        self.state = {"bindings": {"a": self.dennis, "b": self.debbie}}

    def change(self, **kwargs):
        return t.change(self.state, self.dennis, {"command": "tree", "action": "set", "tree_name": "run", **kwargs})

    def test_ownership_atomic_validation_and_roles(self):
        tree = self.change(nodes=[{"id": "a", "agent": "Dennis"}])
        with self.assertRaisesRegex(ValueError, "creating identity"):
            t.change(self.state, self.debbie, {"command": "tree", "action": "delete", "tree_name": "run"})
        for nodes in ([{"id": "a", "parent": "a"}], [{"id": "a", "parent": "missing"}],
                      [{"id": "a", "agent": "Unknown"}], [{"id": "a", "label": "\x1b[31mx"}]):
            with self.assertRaises(ValueError):
                self.change(nodes=nodes)
            self.assertEqual(self.state['display_trees']['run'], tree)
        t.change(self.state, self.debbie, {"command": "role", "value": "worker"})
        self.assertEqual(t.presentation(self.state)['roles']['debbie'], 'worker')
        t.change(self.state, self.debbie, {"command": "role", "value": ""})
        self.assertEqual(t.presentation(self.state)['roles']['debbie'], '')

    def test_render_queue_speaker_arrow_external_and_idle(self):
        self.change(nodes=[{"id": "a", "agent": "Dennis"}, {"id": "b", "parent": "a", "agent": "Debbie"}])
        self.debbie['agent_role'] = 'worker'
        view = t.TreeView('run', color=False)
        view.metadata(t.presentation(self.state), [{"name": "Debbie"}])
        view.update({"name": "Dennis", "phase": "displaying", "recipient": {"kind": "agent", "name": "Debbie"}}, 0)
        output = '\n'.join(view.render(60, 10, 0))
        self.assertIn('▶ Dennis', output)
        self.assertIn('Debbie · worker [1 queued]', output)
        self.assertIn('└▶   └─ Debbie', output)
        view.update({"name": "Dennis", "phase": "preparing", "recipient": {"kind": "user", "name": "Oli"}}, 1)
        self.assertIn('Oli', '\n'.join(view.render(60, 10, 1)))
        view.update(None, 2)
        self.assertNotIn('Oli', '\n'.join(view.render(60, 10, 2)))
        for w in (1, 8, 40):
            for h in (1, 2, 3):
                lines = view.render(w, h, 2)
                self.assertEqual(len(lines), h)
                self.assertTrue(all(conversation.text_width(line) <= w for line in lines))

    def test_info_lines_preserve_arrow_endpoints_and_indent(self):
        self.change(nodes=[{"id": "a", "agent": "Dennis", "info": "Coordinating work"},
                           {"id": "b", "parent": "a", "label": "engine", "info": "New calculator"},
                           {"id": "c", "parent": "b", "agent": "Debbie", "info": "Implementing parser"}])
        view = t.TreeView('run', color=False)
        view.metadata(t.presentation(self.state), [])
        view.update({"name": "Dennis", "phase": "displaying", "recipient": {"kind": "agent", "name": "Debbie"}}, 0)
        lines = view.render(60, 10, 0)
        self.assertIn('Coordinating work', lines[2])
        self.assertIn('New calculator', lines[4])
        self.assertIn('└▶', lines[5])
        self.assertIn('Implementing parser', lines[6])
        self.assertGreater(lines[6].index('Implementing'), lines[4].index('New'))
        for info in ('x' * 161, '\x1b[31mred', None):
            with self.assertRaises(ValueError):
                self.change(nodes=[{"id": "a", "info": info}])

    def test_title_changes_preserve_nodes_and_render_in_header(self):
        original = self.change(nodes=[{"id": "a", "agent": "Dennis"}], title="Calculator implementation")
        view = t.TreeView('run', color=False)
        view.metadata(t.presentation(self.state), [])
        view.update(None, 0)
        self.assertIn('Calculator implementation · live', view.render(60, 4, 0)[0])
        self.change(title='Parser review')
        self.assertEqual(self.state['display_trees']['run']['nodes'], original['nodes'])
        self.change(nodes=original['nodes'])
        self.assertEqual(self.state['display_trees']['run']['title'], 'Parser review')
        for title in ('x' * 161, '\x1b[31mred', None):
            with self.assertRaises(ValueError):
                self.change(title=title)
            self.assertEqual(self.state['display_trees']['run']['title'], 'Parser review')
        self.change(title='')
        view.metadata(t.presentation(self.state), [])
        self.assertIn('run · live', view.render(60, 4, 0)[0])
        self.assertLessEqual(conversation.text_width(view.render(8, 1, 0)[0]), 8)

    def test_conversation_roles(self):
        view = conversation.ConversationView(color=False)
        view.metadata({'roles': {'dennis': 'supervisor'}}, [])
        self.assertEqual(view.name_label({'kind': 'agent', 'name': 'Dennis'}), 'Dennis · supervisor')
        self.assertEqual(view.name_label({'kind': 'user', 'name': 'Dennis'}), 'Dennis')


from test_agent_comms import Fixture
import json
import agent_comms as app


class TreeProtocolTests(Fixture):
    def test_role_tree_ownership_queue_snapshot_and_persistence(self):
        sender = self.sender('Alice')
        other = self.sender('Bob')
        self.start_server()
        owner = {'pane_env': sender['pane_env']}
        def request(command, **data):
            _, stream = self.request(command, **data)
            result = json.loads(stream.readline())
            if result.get("ok"):
                self.assertEqual(result["protocol_version"], app.VERSION)
            return result
        self.assertTrue(request('role', value='supervisor', **owner)['ok'])
        nodes = [{'id': 'root', 'agent': 'Alice'}, {'id': 'worker', 'parent': 'root', 'agent': 'Bob'}]
        self.assertTrue(request('tree', action='set', tree_name='run', nodes=nodes, title='Implementation', **owner)['ok'])
        denied = request('tree', action='delete', tree_name='run', pane_env=other['pane_env'])
        self.assertFalse(denied['ok'])
        _, stream = self.request('watch', presentation=True)
        initial = json.loads(stream.readline())
        self.assertEqual(initial['presentation']['roles']['alice'], 'supervisor')
        self.assertEqual(initial['presentation']['trees']['run']['title'], 'Implementation')
        self.assertEqual(initial['presentation']['trees']['run']['nodes'],
                         [dict(nodes[0], label='Alice'), dict(nodes[1], label='Bob')])
        request('send', **sender, msg='hello')
        queued = json.loads(stream.readline())
        self.assertEqual(queued['queued'], [{'name': 'Alice'}])
        # A newly constructed listener gets the persisted metadata without clients
        # replaying role assignments or topology.
        server = app.Server(self.paths, app.load_config(self.paths.config))
        self.assertEqual(server.snapshot()['presentation'], initial['presentation'])
        missing = request('role', value='worker', pane_env=self.pane_env('unallocated'))
        self.assertFalse(missing['ok'])


if __name__ == '__main__':
    unittest.main()
