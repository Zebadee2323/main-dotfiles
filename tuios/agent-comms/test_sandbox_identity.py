"""Identity RPC with a real isolated listener and a client denied state writes."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import AsyncMock, patch

import agent_comms as app
import tuios_context
from test_agent_comms import Fixture, ROOT


@unittest.skipIf(os.name == 'nt', 'fake tuios executable uses a POSIX shebang')
class SandboxIdentityTests(Fixture):
    def setUp(self):
        super().setUp()
        bin_dir = self.paths.state.parent / 'bin'
        bin_dir.mkdir()
        tuios = bin_dir / 'tuios'
        tuios.write_text(f'#!{sys.executable}\n' + '''import json, os, sys
pane = os.environ.get('TUIOS_PANE_ID', '')
window = dict(window_id=pane, workspace=1, display_name='Codex', agent_state='working')
command = sys.argv[1]
if command == 'session-info':
    data = dict(session_name=os.environ['TUIOS_SESSION'], tui_attached=True)
elif command == 'list-windows' and '--all' in sys.argv:
    data = dict(success=True, total=1, panes=[window])
elif command == 'list-windows':
    data = dict(windows=[window], current_workspace=1, focused_window_id=pane)
elif command == 'get-window':
    data = dict(success=True, id=pane, display_name='Codex', title='Codex')
else:
    data = dict(success=True)
print(json.dumps(data))
''')
        tuios.chmod(0o700)
        self.env['PATH'] = str(bin_dir) + os.pathsep + self.env.get('PATH', '')
        # The listener deliberately has no pane of its own.
        self.start_server()
        self.env.update(TUIOS_ENV='1', TUIOS_PANE_ID='caller', TUIOS_SESSION='test')

    def restricted_cli(self, *args):
        # Enforce the filesystem boundary in a separate client process. The
        # listener remains able to persist state. Audit hooks also cover os.open.
        script = '''import os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import agent_comms as app
state = Path(os.environ['AGENT_COMMS_STATE_DIR']).resolve()
def denied_path(value):
    return isinstance(value, (str, bytes, os.PathLike)) and Path(os.fsdecode(value)).resolve().is_relative_to(state)
def audit(event, values):
    if event == 'open' and denied_path(values[0]):
        if values[2] & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC):
            raise PermissionError('client state writes denied')
    if event in ('os.mkdir', 'os.chmod', 'os.remove', 'os.rename') and denied_path(values[0]):
        raise PermissionError('client state writes denied')
sys.addaudithook(audit)
try:
    (state / 'probe').write_text('must fail')
except PermissionError:
    pass
else:
    raise AssertionError('write guard failed')
sys.exit(app.main(sys.argv[2:]))
'''
        return subprocess.run([sys.executable, '-c', script, str(ROOT), *args], env=self.env,
                              capture_output=True, text=True, timeout=10)

    def test_lookup_send_and_repeat_work_without_any_client_state_writes(self):
        first = self.restricted_cli('context')
        self.assertEqual(first.returncode, 0, first.stderr)
        identity = json.loads(first.stdout)
        self.assertEqual(set(identity), {'id', 'name', 'prefix'})
        saved = json.loads((self.paths.state / 'pane-identities.json').read_text())
        self.assertEqual(next(iter(saved['bindings'].values()))['pane_id'], 'caller')
        second = self.restricted_cli('context')
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(json.loads(second.stdout), identity)
        sent = self.restricted_cli('send', '--id', identity['id'], '--msg', 'Ready.')
        self.assertEqual(sent.returncode, 0, sent.stderr)
        history = self.cli('history', '--json')
        self.assertEqual(json.loads(history.stdout)[-1]['msg'], 'Ready.')
        wrong = self.restricted_cli('send', '--id', 'stale', '--msg', 'Wrong.')
        self.assertEqual(wrong.returncode, 1)
        self.assertIn('Identity mismatch', wrong.stderr)
        line = next(line for line in wrong.stderr.splitlines() if line.startswith('agent-comms: Fresh context: '))
        self.assertEqual(json.loads(line.removeprefix('agent-comms: Fresh context: ')), identity)
        self.assertEqual(len(json.loads(self.cli('history', '--json').stdout)), 1)

    def test_server_rejects_arbitrary_environment_and_unknown_pane_without_allocation(self):
        for env in ({**{k: self.env[k] for k in app.PANE_ENV_KEYS if k in self.env}, 'PATH': '/bad'},
                    {'TUIOS_ENV': '1', 'TUIOS_SESSION': 'test'}):
            _, stream = self.request('context', pane_env=env)
            self.assertFalse(json.loads(stream.readline())['ok'])
        self.assertFalse((self.paths.state / 'pane-identities.json').exists())

    def test_clear_and_reallocate_without_client_state_writes(self):
        first = self.restricted_cli('context')
        self.assertEqual(first.returncode, 0, first.stderr)
        identity = json.loads(first.stdout)
        bag = (self.paths.state / 'bag.json').read_text()
        cleared = self.restricted_cli('context', '--clear')
        self.assertEqual(cleared.returncode, 0, cleared.stderr)
        self.assertEqual(json.loads(cleared.stdout), {'cleared': True, 'id': identity['id']})
        self.assertEqual(json.loads((self.paths.state / 'pane-identities.json').read_text())['bindings'], {})
        self.assertEqual((self.paths.state / 'bag.json').read_text(), bag)
        repeated = self.restricted_cli('context', '--clear')
        self.assertEqual(repeated.returncode, 0, repeated.stderr)
        self.assertEqual(json.loads(repeated.stdout), {'cleared': False, 'id': None})
        sent = self.restricted_cli('send', '--id', identity['id'], '--msg', 'Ready.')
        self.assertEqual(sent.returncode, 1)
        self.assertIn('no current identity', sent.stderr)
        lookup = self.restricted_cli('context')
        self.assertEqual(lookup.returncode, 0, lookup.stderr)
        self.assertEqual(set(json.loads(lookup.stdout)), {'id', 'name', 'prefix'})


class ContextClientTests(Fixture):
    def test_clear_requires_upgraded_listener(self):
        import asyncio
        env = {'TUIOS_ENV': '1', 'TUIOS_PANE_ID': 'caller', 'TUIOS_SESSION': 'work'}
        with patch.dict(os.environ, env), patch.object(app, 'checked_context', return_value={}), \
             patch.object(app, 'endpoint', return_value={'pane_identity': True}), \
             patch.object(app, 'rpc', new_callable=AsyncMock) as rpc:
            with self.assertRaisesRegex(app.CommsError, 'restart'):
                asyncio.run(app.listener_context(self.paths, clear=True))
            rpc.assert_not_called()

    def test_context_uses_client_prefix_policy_with_stale_listener(self):
        import asyncio
        env = {'TUIOS_ENV': '1', 'TUIOS_PANE_ID': 'caller', 'TUIOS_SESSION': 'work'}
        source = {'session_name': 'work', 'workspace': 1, 'introduce': True,
                  'position': 'bottom-right', 'position_reference': 'workspace'}
        identity = {'id': 'a', 'name': 'Alice', 'prefix': 'Alice here, of the work session.'}
        with patch.dict(os.environ, env), patch.object(app, 'checked_context', return_value=source), \
             patch.object(app, 'endpoint', return_value={'pane_identity': True}), \
             patch.object(app, 'rpc', new_callable=AsyncMock, return_value={'identity': identity}):
            result = asyncio.run(app.listener_context(self.paths))
        self.assertEqual(result, {**identity, 'prefix':
                         'Alice here, in the work session, workspace 1, in the bottom-right.'})
        app.validate_message(app.load_config(self.paths.config), 'a', result['prefix'] + ' Ready.', source=source)

    def test_launchd_path_discovers_homebrew_tuios_before_running_queries(self):
        import shutil
        minimal = {'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'TUIOS_ENV': '1',
                   'TUIOS_PANE_ID': 'caller', 'TUIOS_SESSION': 'test'}
        listing = {'windows': [{'window_id': 'caller', 'workspace': 1}],
                   'current_workspace': 1, 'focused_window_id': 'caller'}
        run = unittest.mock.Mock(side_effect=[
            subprocess.CompletedProcess([], 0, json.dumps({'tui_attached': True})),
            subprocess.CompletedProcess([], 0, json.dumps(listing)),
        ])
        def which(name, *, path):
            return '/opt/homebrew/bin/tuios' if path == '/opt/homebrew/bin:/usr/local/bin' else None
        with patch.object(tuios_context.sys, 'platform', 'darwin'), patch.object(shutil, 'which', side_effect=which):
            source = tuios_context.context('', env=minimal, run=run, strict=True)
        self.assertEqual(source['pane_id'], 'caller')
        for call in run.call_args_list:
            self.assertEqual(call.args[0][0], '/opt/homebrew/bin/tuios')
            self.assertEqual(call.kwargs['env']['PATH'], minimal['PATH'])

    def test_missing_executable_and_denial_have_distinct_errors_without_retry(self):
        env = {'TUIOS_ENV': '1', 'TUIOS_PANE_ID': 'caller', 'TUIOS_SESSION': 'test'}
        for error, message in ((FileNotFoundError(), 'executable was not found'),
                               (PermissionError(), 'access denied')):
            run = unittest.mock.Mock(side_effect=error)
            with patch.object(tuios_context, 'executable', return_value='tuios'):
                with self.assertRaisesRegex(tuios_context.ContextError, message):
                    tuios_context.context('', env=env, run=run, strict=True)
            run.assert_called_once()

    def test_local_context_denial_is_not_retried_via_listener(self):
        import asyncio
        env = {'TUIOS_ENV': '1', 'TUIOS_PANE_ID': 'caller', 'TUIOS_SESSION': 'test'}
        with patch.dict(os.environ, env), patch.object(app, 'checked_context', side_effect=app.CommsError('forbidden')), \
             patch.object(app, 'rpc', new_callable=AsyncMock) as rpc:
            with self.assertRaisesRegex(app.CommsError, 'forbidden'):
                asyncio.run(app.listener_context(self.paths))
            rpc.assert_not_called()

    def test_old_listener_requires_restart_without_client_allocation(self):
        import asyncio
        env = {'TUIOS_ENV': '1', 'TUIOS_PANE_ID': 'caller', 'TUIOS_SESSION': 'test'}
        with patch.dict(os.environ, env), patch.object(app, 'checked_context', return_value={}), \
             patch.object(app, 'endpoint', return_value={}), patch.object(app, 'rpc', new_callable=AsyncMock) as rpc:
            with self.assertRaisesRegex(app.CommsError, 'restart'):
                asyncio.run(app.listener_context(self.paths))
            rpc.assert_not_called()
            self.assertFalse(self.paths.state.exists())
