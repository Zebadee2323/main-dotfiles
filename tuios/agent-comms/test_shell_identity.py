"""Standalone shell ownership, durable bindings, and sandbox-compatible RPC."""
import asyncio
import json
import os
import subprocess
import sys
import unittest
from unittest.mock import AsyncMock, Mock, patch

import agent_comms as app
import pane_identity
import shell_identity as shell
from test_agent_comms import Fixture, ROOT


class DiscoveryTests(unittest.TestCase):
    def table(self):
        return {100: (1, 'start-a', '/bin/zsh', '-zsh'),
                200: (100, 'agent-start', 'codex', 'codex'),
                300: (200, 'tool-start', '/bin/zsh', 'zsh -lc agent-comms context'),
                400: (300, 'cli-start', 'python', 'python agent_comms.py context')}

    def test_calls_below_transient_shells_share_persistent_shell(self):
        table = self.table()
        with patch.object(shell, 'processes', return_value=table):
            first = shell.discover(400)
            table[500] = (200, 'tool-two', '/bin/bash', 'bash -c agent-comms context')
            self.assertEqual(first, shell.discover(500))
            self.assertEqual(first, shell.token(100, table[100]))
            table[600] = (1, 'start-b', 'zsh', 'zsh')
            self.assertNotEqual(first, shell.discover(600))
            table[100] = (1, 'replacement-shell', 'zsh', 'zsh')
            self.assertNotEqual(first, shell.discover(400))

    def test_nested_shell_is_distinct_and_missing_or_cyclic_ancestry_fails(self):
        table = self.table()
        table[300] = (200, 'nested', 'bash', 'bash')
        with patch.object(shell, 'processes', return_value=table):
            self.assertEqual(shell.discover(400), shell.token(300, table[300]))
            for rows in ({}, {400: (400, 'cycle', 'python', 'python')}):
                with patch.object(shell, 'processes', return_value=rows):
                    with self.assertRaisesRegex(ValueError, 'No persistent parent shell'):
                        shell.discover(400)

    def test_windows_persistent_shell_options_and_command_wrappers(self):
        for name in ('powershell.exe', 'pwsh.exe'):
            self.assertTrue(shell.is_shell((1, 'start', name, name + ' -ExecutionPolicy Bypass -NoProfile')))
            self.assertFalse(shell.is_shell((1, 'start', name, name + ' -Command agent-comms')))
        self.assertTrue(shell.is_shell((1, 'start', 'cmd.exe', 'cmd.exe /k')))
        self.assertFalse(shell.is_shell((1, 'start', 'cmd.exe', 'cmd.exe /c agent-comms')))

    def test_denied_snapshot_never_retries(self):
        run = Mock(side_effect=PermissionError('denied'))
        with self.assertRaisesRegex(ValueError, 'Cannot inspect shell processes'):
            shell.processes(run)
        run.assert_called_once()

    @unittest.skipIf(os.name == 'nt', 'POSIX ps format')
    def test_ps_creation_date_and_command_are_parsed(self):
        run = Mock(return_value=subprocess.CompletedProcess([], 0,
            ' 100 1 Wed Oct 28 10:02:33 2026 /bin/zsh -zsh\n'))
        with patch.object(shell.os.path, 'isdir', return_value=False):
            table = shell.processes(run)
        self.assertEqual(table[100], (1, 'Wed Oct 28 10:02:33 2026', '/bin/zsh', '-zsh'))
        self.assertTrue(shell.is_shell(table[100]))


class ShellBindingTests(Fixture):
    def test_bindings_survive_listener_restart_and_clear_without_tuios_queries(self):
        config = app.load_config(self.paths.config)
        env = {shell.ENV_KEY: '123:' + 'a' * 64}
        other = {shell.ENV_KEY: '456:' + 'b' * 64}
        with patch.object(shell, 'inventory', return_value={env[shell.ENV_KEY], other[shell.ENV_KEY]}), \
                patch.object(pane_identity, 'inventory', side_effect=AssertionError('tuios called')):
            first = app.identify(self.paths, config, env)
            self.assertEqual(first['prefix'], '')
            self.assertEqual(first, app.identify(self.paths, app.load_config(self.paths.config), env))
            second = app.identify(self.paths, config, other)
            self.assertNotEqual(first['id'], second['id'])
            self.assertEqual(app.pane_binding(self.paths, config, allocate=False, env=env), first['id'])
            cleared = app.pane_binding(self.paths, config, clear=True, env=env)
            self.assertEqual(cleared, {'cleared': True, 'id': first['id']})
            with self.assertRaisesRegex(app.ContextMismatch, 'no current identity'):
                app.pane_binding(self.paths, config, allocate=False, env=env)
            self.assertEqual(app.identify(self.paths, config, other), second)

    def test_listener_derives_owner_and_ignores_its_own_tuios_environment(self):
        with patch.dict(os.environ, {'TUIOS_ENV': '1', 'TUIOS_PANE_ID': 'listener'}), \
                patch.object(shell, 'discover', return_value='123:' + 'a' * 64) as discover:
            env = app.request_owner_env({'shell_pid': 123})
            discover.assert_called_once_with(123)
            self.assertNotIn('TUIOS_ENV', env)
            self.assertEqual(env[shell.ENV_KEY], '123:' + 'a' * 64)
            for value in (None, True, 0, -1, '123', 2 ** 32):
                with self.assertRaises(app.CommsError):
                    app.request_owner_env({'shell_pid': value})
            with self.assertRaises(app.CommsError):
                app.request_owner_env({'shell_pid': 123, 'pane_env': {'TUIOS_ENV': '1'}})

    def test_client_requires_new_listener_without_inspecting_processes(self):
        with patch.dict(os.environ, {}, clear=True), \
                patch.object(shell, 'processes', side_effect=AssertionError('client ps')), \
                patch.object(app, 'endpoint', return_value={'pane_identity': True}), \
                patch.object(app, 'rpc', new_callable=AsyncMock) as rpc:
            with self.assertRaisesRegex(app.CommsError, 'restart'):
                asyncio.run(app.listener_context(self.paths))
            rpc.assert_not_called()

    def test_shell_pressure_reclaims_dead_shell_but_never_live_or_tuios(self):
        config = app.load_config(self.paths.config)
        owners = [f'{i}:' + str(i) * 64 for i in range(1, 5)]
        live = set(owners[:3])
        with patch.object(shell, 'inventory', side_effect=lambda: live), \
                patch.object(pane_identity, 'inventory', side_effect=AssertionError('tuios called')):
            ids = [app.identify(self.paths, config, {shell.ENV_KEY: o})['id'] for o in owners[:3]]
            live.add(owners[3])
            with self.assertRaisesRegex(app.CommsError, 'No unassigned'):
                app.identify(self.paths, config, {shell.ENV_KEY: owners[3]})
            live.remove(owners[0])
            new = app.identify(self.paths, config, {shell.ENV_KEY: owners[3]})
            self.assertEqual(new['id'], ids[0])


@unittest.skipIf(os.name == 'nt', 'integration uses a persistent POSIX shell')
class StandaloneIntegrationTests(Fixture):
    def setUp(self):
        super().setUp()
        try:
            shell.processes()
        except ValueError as exc:
            self.skipTest(str(exc))
        self.server, _ = self.start_server()

    def caller(self):
        process = subprocess.Popen(['/bin/bash', '--noprofile', '--norc'], env=self.env,
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True)
        self.addCleanup(self.cleanup_process, process)
        self.addCleanup(process.stdin.close)
        return process

    def call(self, process, *args):
        # Audit hooks prove the standalone client neither inspects processes nor
        # writes state. Its listener performs those operations on its behalf.
        code = '''import sys, os
sys.path.insert(0, sys.argv[1])
import agent_comms as app
def audit(event, values):
    if event == 'subprocess.Popen': raise PermissionError('client process queries denied')
    if event == 'open' and values[2] & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC):
        raise PermissionError('client writes denied')
sys.addaudithook(audit)
sys.exit(app.main(sys.argv[2:]))
'''
        import shlex
        command = shlex.join([sys.executable, '-c', code, str(ROOT), *args])
        # Capture stderr per command in the same pipe without shell redirection files.
        process.stdin.write(command + ' 2>&1\nprintf "__DONE__%s\\n" "$?"\n')
        process.stdin.flush()
        lines = []
        while True:
            line = process.stdout.readline()
            self.assertTrue(line, 'caller exited unexpectedly')
            if line.startswith('__DONE__'):
                return int(line.removeprefix('__DONE__')), ''.join(lines)
            lines.append(line)

    def test_separate_shells_repeat_send_mismatch_clear_and_listener_restart(self):
        caller, other = self.caller(), self.caller()
        status, output = self.call(caller, 'context')
        self.assertEqual(status, 0, output)
        first = json.loads(output)
        self.assertEqual(first['prefix'], '')
        self.assertEqual(self.call(caller, 'context'), (0, output))
        status, output = self.call(other, 'context')
        self.assertEqual(status, 0, output)
        self.assertNotEqual(first['id'], json.loads(output)['id'])
        status, output = self.call(caller, 'send', '--id', first['id'], '--msg', 'Ready.')
        self.assertEqual(status, 0, output)
        status, output = self.call(caller, 'send', '--id', 'stale', '--msg', 'Wrong.')
        self.assertEqual(status, 1, output)
        self.assertIn('Fresh context:', output)
        self.assertEqual(len(json.loads(self.cli('history', '--json').stdout)), 1)
        self.cleanup_process(self.server)
        self.start_server()
        status, output = self.call(caller, 'context')
        self.assertEqual(status, 0, output)
        self.assertEqual(json.loads(output), first)
        status, output = self.call(caller, 'context', '--clear')
        self.assertEqual(status, 0, output)
        self.assertEqual(json.loads(output), {'cleared': True, 'id': first['id']})
        status, output = self.call(caller, 'send', '--id', first['id'], '--msg', 'Stale.')
        self.assertEqual(status, 1, output)
        self.assertIn('no current identity', output)
