"""Log arrivals, queue/playback states, reconnects, and silent event subscription."""
import asyncio
import io
import json
import time
from unittest.mock import patch

import agent_comms as app
import history_view as h
from test_history import record
from test_agent_comms import Fixture, write_config


class LogTests(Fixture):
    def test_first_live_arrival_animates_but_snapshots_and_duplicates_do_not(self):
        view = h.LogView(color=False)
        view.update([], 0, snapshot=True)
        message = record('hello', uid='first')
        view.receive([message], {'message':None, 'queued':[{'uid':'first'}]}, 1)
        self.assertTrue(view.animating(1))
        view.receive([message], {'message':None, 'queued':[{'uid':'first'}]}, 2)
        self.assertFalse(view.animating(2))
        self.assertIn('[queued]', '\n'.join(view.render(70,12,2)))
        current = dict(message, phase='displaying', started_at=2, duration=2)
        view.receive([message], {'message':current, 'queued':[]}, 2)
        early = view.render(70,12,2)
        late = view.render(70,12,3)
        self.assertIn('[▶ speaking]', '\n'.join(early))
        self.assertNotEqual(early, late, 'playback progress is dynamic')
        view.disconnect()
        self.assertIn('offline', view.render(70,12,4)[0])
        self.assertIn('hello', '\n'.join(view.render(70,12,4)))
        view.receive([message, record('caught up', uid='second')], {'message':None}, 5, snapshot=True)
        self.assertFalse(view.animating(5))
        for width,height in ((1,1),(8,2),(40,10)):
            self.assertEqual(len(view.render(width,height,5)),height)

    def test_log_receives_acceptance_and_lifecycle_and_recovers_after_restart(self):
        write_config(self.paths.config, 'display_seconds = 0.3\n[user]\nname = "Oli"\n')
        sender = self.sender('Alice')
        process, _ = self.start_server()
        output = io.StringIO()
        async def until(predicate):
            deadline = time.monotonic() + 4
            while not predicate():
                if time.monotonic() > deadline:
                    self.fail('log watcher did not receive expected update: ' + output.getvalue()[-1500:])
                await asyncio.sleep(.03)
        async def run():
            task = asyncio.create_task(app.watch_log(self.paths, color=False, animate=False,
                                                     size=lambda:(90,24), writer=output))
            try:
                await until(lambda: '○ idle' in output.getvalue())
                for text in ('first arrival', 'second arrival'):
                    _, stream = await asyncio.to_thread(self.request, 'send', **sender, msg=text)
                    self.assertTrue(json.loads(await asyncio.to_thread(stream.readline))['ok'])
                await until(lambda: 'second arrival' in output.getvalue() and '[queued]' in output.getvalue())
                await until(lambda: '[▶ speaking]' in output.getvalue())
                process.terminate()
                await asyncio.to_thread(process.wait, 5)
                await until(lambda: 'offline' in output.getvalue())
                output.seek(0); output.truncate()
                await asyncio.to_thread(self.start_server)
                await until(lambda: '○ idle' in output.getvalue())
                self.assertIn('first arrival', output.getvalue())
                self.assertIn('second arrival', output.getvalue())
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        with patch.object(app, 'watcher_speech', side_effect=AssertionError('log must be silent')), \
                patch.object(app.speech, 'play_live', side_effect=AssertionError('no playback')):
            asyncio.run(run())

    def test_log_participates_in_watcher_reload(self):
        async def run():
            async def log(*a, **k):
                await asyncio.Future()
            with patch.object(app, 'watch_log', side_effect=log), \
                    patch.object(app, 'watcher_revision', side_effect=['before','after']):
                self.assertTrue(await app.watch(self.paths, color=False, width=80, view='log'))
        asyncio.run(run())
