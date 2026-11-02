"""Mouse parsing, exact-pane navigation, queue previews, and history arrivals."""
import json
import os
import sys
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import conversation as c
import history_view as h
import pane_identity
import tree_view
import watcher_input as w
from test_history import record


class InteractionTests(unittest.TestCase):
    def test_fragmented_clicks_ignore_release_wheel_and_other_buttons(self):
        input = w.Input(123)
        packets = [b'\x1b[<0;12;', b'4M\x1b[<0;12;4m\x1b[<64;1;1M\x1b[<2;1;1MQ']
        with patch('select.select', return_value=([123], [], [])), patch('os.read', side_effect=packets):
            self.assertEqual(input.events(), [])
            self.assertEqual(input.events(), [('click', 11, 3), ('queue',)])

    @unittest.skipIf(os.name == 'nt', 'POSIX terminal attributes')
    def test_terminal_modes_are_restored_when_watcher_fails(self):
        import pty
        import termios
        master, slave = pty.openpty()
        reader = os.fdopen(os.dup(slave), 'r')
        writer = os.fdopen(os.dup(slave), 'w', buffering=1)
        before = termios.tcgetattr(slave)
        try:
            with patch.object(sys, 'stdin', reader), patch.object(sys, 'stdout', writer):
                with self.assertRaises(RuntimeError):
                    with w.terminal_input(mouse=True):
                        self.assertFalse(termios.tcgetattr(slave)[3] & termios.ICANON)
                        raise RuntimeError('watcher failed')
            after = termios.tcgetattr(slave)
            # macOS can set PENDIN itself while applying restored attributes.
            pendin = getattr(termios, 'PENDIN', 0)
            before[3] &= ~pendin
            after[3] &= ~pendin
            self.assertEqual(after, before)
            output = os.read(master, 4096)
            self.assertIn(b'\x1b[?1000h', output)
            self.assertIn(b'\x1b[?1000l', output)
        finally:
            reader.close()
            writer.close()
            os.close(master)
            os.close(slave)

    def test_focus_verifies_pane_and_stops_on_denial(self):
        env = {'TUIOS_ENV': '1', 'TUIOS_PANE_ID': 'watcher', 'TUIOS_SESSION': 'home'}
        binding = {'daemon': pane_identity.owner(env)[1], 'session': 'workers', 'pane': 'exact-pane'}
        calls = []
        def run(argv, **kwargs):
            calls.append(argv[1:])
            return SimpleNamespace(returncode=0, stdout=json.dumps({'windows': [{'window_id':'exact-pane'}]}), stderr='')
        w.focus_agent(binding, env=env, run=run)
        self.assertEqual(calls, [['list-windows', '-s', 'workers', '--json'],
                                ['focus-window', '-s', 'workers', 'exact-pane'],
                                ['switch-session', '-s', 'home', 'workers']])
        calls.clear()
        def denied(argv, **kwargs):
            calls.append(argv)
            return SimpleNamespace(returncode=1, stdout='', stderr='forbidden')
        with self.assertRaisesRegex(ValueError, 'forbidden'):
            w.focus_agent(binding, env=env, run=denied)
        self.assertEqual(len(calls), 1)
        with self.assertRaisesRegex(ValueError, 'daemon'):
            w.focus_agent(dict(binding, daemon='other'), env=env, run=run)
        self.assertEqual(len(calls), 1)

    def test_navigation_timeout_and_stale_panes_do_not_focus(self):
        env = {'TUIOS_ENV': '1', 'TUIOS_PANE_ID': 'watcher', 'TUIOS_SESSION': 'home'}
        binding = {'daemon': pane_identity.owner(env)[1], 'session': 'workers', 'pane': 'gone'}
        with self.assertRaisesRegex(ValueError, 'no longer present'):
            w.focus_agent(binding, env=env, run=lambda *a, **k: SimpleNamespace(
                returncode=0, stdout='{"windows": []}', stderr=''))
        with self.assertRaisesRegex(ValueError, 'Navigation failed'):
            w.focus_agent(binding, env=env, run=lambda *a, **k: (_ for _ in ()).throw(
                subprocess.TimeoutExpired('tuios', 3)))

    def test_hit_regions_follow_visible_agent_rows_and_exclude_info(self):
        view = tree_view.TreeView('test', color=False)
        binding = {'pane':'real-pane', 'session':'workers', 'daemon':'d'}
        view.metadata({'agents': {'alice':binding}, 'trees': {'test': {'nodes': [
            {'id':'group', 'label':'engine'},
            {'id':'a', 'parent':'group', 'agent':'Alice', 'label':'Alice', 'info':'Task'}]}}}, [])
        view.update(None, 0)
        view.render(60, 10, 0)
        self.assertIsNone(view.hit_test(10, 1))
        self.assertEqual(view.hit_test(8, 2), binding)
        self.assertIsNone(view.hit_test(8, 3))
        self.assertIsNone(view.hit_test(59, 2))
        view.update(None, 1, offline=True)
        self.assertIsNone(view.hit_test(8, 2))

    def test_conversation_queue_preview_and_expansion_fit(self):
        view = c.ConversationView(color=False, animate=False)
        view.metadata({}, [{'name':'Alice', 'msg':'Review the parser please', 'wait_seconds':3.2,
                            'recipient':{'name':'Bob'}}, {'name':'Bob', 'msg':'Done', 'wait_seconds':8.4}], now=0)
        view.update(None, 0)
        preview = '\n'.join(view.render(70, 24, 0))
        self.assertIn('2 queued', preview)
        self.assertIn('Alice → Bob', preview)
        self.assertIn('Review the parser please', preview)
        self.assertIn('~3s', preview)
        self.assertIn('next in ~1s', '\n'.join(view.render(70,24,2)))
        view.expanded_queue = True
        for width, height in ((70,24),(20,8),(1,1)):
            lines = view.render(width,height,1)
            self.assertEqual(len(lines), height)
            self.assertTrue(all(c.text_width(line) <= width for line in lines))
        view.update(None, 0, offline=True)
        self.assertNotIn('Up next', '\n'.join(view.render(70,24,0)))


class HistoryTests(unittest.TestCase):
    def test_initial_snapshot_is_still_new_messages_slide_and_reset_does_not(self):
        view = h.LogView(color=False)
        first, second = record('hello', uid='first'), record('new message', uid='second')
        view.update([first], 10)
        self.assertFalse(view.animating(10))
        view.update([first,second], 11)
        self.assertTrue(view.animating(11))
        early = view.render(70,18,11)
        late = view.render(70,18,12)
        self.assertEqual(early[0], late[0], 'header does not slide')
        self.assertNotEqual(early, late)
        self.assertGreater(next(line.index('new message') for line in early if 'new message' in line),
                           next(line.index('new message') for line in late if 'new message' in line))
        view.update([record('reset', uid='reset')], 13, snapshot=True)
        self.assertFalse(view.animating(13))

    def test_history_wraps_unicode_and_sanitizes_and_respects_no_animation(self):
        message = record('界' * 30 + '\x1b[2J', name='\x1b[31mAlice', agent_role='worker',
                         recipient={'name':'Bob'})
        for width in (1, 8, 40, 80):
            result = h.document([message], width=width)
            self.assertTrue(all(c.text_width(line) <= width for line in result.splitlines()))
            self.assertNotIn('\x1b', result)
        pretty = h.document([message], width=80)
        self.assertIn('Alice · worker → Bob', pretty)
        self.assertIn('Sat 31 Oct 2026', pretty)
        view = h.LogView(animate=False)
        view.update([message],0)
        view.update([message,record('second')],1)
        self.assertFalse(view.animating(1))


if __name__ == '__main__':
    unittest.main()
