import asyncio
import io
import json
import os
from unittest.mock import AsyncMock, Mock, patch

import agent_comms as app
from test_agent_comms import TEST_PANE, Fixture, seed_binding

EPOCH = "e" * 16


class SendTitleTests(Fixture):
    # A fake pane on a nonexistent tuios socket: never the user's live daemon.
    own_env = {**TEST_PANE, "TUIOS_PANE_ID": "own", "TUIOS_SESSION": "work"}

    def setUp(self):
        super().setUp()
        self.epoch = seed_binding(self.paths, self.own_env, "Alice")

    def server(self):
        return app.Server(self.paths, app.load_config(self.paths.config), quiet=True)

    async def submit(self, server, **fields):
        reader = asyncio.StreamReader()
        reader.feed_data(json.dumps({"protocol_version": app.VERSION, "token": server.token,
            "command": "send", "name": "Alice", "epoch": self.epoch, "msg": "Ready.",
            "pane_env": self.own_env, **fields}).encode() + b"\n")
        reader.feed_eof()
        writer = Mock(drain=AsyncMock(), wait_closed=AsyncMock())
        await asyncio.wait_for(server.handle(reader, writer), 1)
        return json.loads(writer.write.call_args_list[0].args[0])

    def test_send_acknowledges_without_waiting_for_title_and_coalesces_per_pane(self):
        async def exercise():
            server = self.server()
            release = asyncio.Event()

            async def slow_thread(function, *args):
                if function == server.refresh_sender_title:
                    await release.wait()
                    return None
                return function(*args)

            with patch.object(app.asyncio, "to_thread", side_effect=slow_thread) as worker:
                try:
                    self.assertTrue((await self.submit(server))["ok"])
                    self.assertTrue((await self.submit(server))["ok"])
                    self.assertEqual(server.queue.qsize(), 2)
                    self.assertEqual(len(server.title_refreshes), 1)
                    refreshes = [c for c in worker.call_args_list if c.args[0] == server.refresh_sender_title]
                    self.assertEqual(len(refreshes), 1)
                    self.assertEqual(refreshes[0].args[2], "Alice")
                finally:
                    release.set()
                    await asyncio.gather(*server.title_refreshes.values())
                await asyncio.sleep(0)
                self.assertEqual(server.title_refreshes, {})
        asyncio.run(exercise())

    def test_rejected_send_does_not_refresh_title(self):
        async def exercise():
            server = self.server()
            with patch.object(server, "schedule_title_refresh") as schedule:
                for fields in ({"msg": ""}, {"pane_env": None}, {"pane_env": {**self.own_env, "PATH": "/bad"}},
                               {"name": "Bob"}, {"epoch": "0" * 16}):
                    with self.subTest(fields=fields):
                        self.assertFalse((await self.submit(server, **fields))["ok"])
                schedule.assert_not_called()
            self.assertEqual(server.queue.qsize(), 0)
            self.assertEqual(server.title_refreshes, {})
        asyncio.run(exercise())

    def test_invalid_optional_title_environment_is_nonfatal(self):
        async def exercise():
            server = self.server()
            with patch.object(app.asyncio, "to_thread") as worker:
                for pane_env in (None, {**self.own_env, "PATH": "/bad"}, {"TUIOS_ENV": "1"}):
                    with self.subTest(pane_env=pane_env):
                        server.schedule_title_refresh("Alice", pane_env)
                worker.assert_not_called()
                self.assertEqual(server.title_refreshes, {})
        asyncio.run(exercise())

    def test_listener_rechecks_binding_and_live_context_without_allocating(self):
        server = self.server()
        source = {"pane_id": "own", "pane_title": "New task"}
        with patch.object(app, "pane_binding", return_value=(EPOCH, {"name": "Alice"})) as binding, \
             patch.object(app, "checked_context", return_value=source) as context, \
             patch.object(app, "update_pane_title") as update:
            server.refresh_sender_title(self.own_env, "Alice")
            binding.assert_called_once_with(self.paths, server.config, allocate=False, env=self.own_env)
            context.assert_called_once_with(self.own_env)
            update.assert_called_once_with(self.paths, server.config, "Alice", source, self.own_env)
            update.reset_mock()
            context.reset_mock()
            binding.return_value = (EPOCH, {"name": "Bob"})
            server.refresh_sender_title(self.own_env, "Alice")
            context.assert_not_called()
            update.assert_not_called()

    def test_listener_naming_failures_are_best_effort(self):
        server = self.server()
        with patch.object(app, "pane_binding", return_value=(EPOCH, {"name": "Alice"})), \
             patch.object(app, "checked_context", side_effect=PermissionError("denied")), \
             patch("sys.stderr", new_callable=io.StringIO) as errors:
            server.refresh_sender_title(self.own_env, "Alice")
            self.assertIn("pane name unchanged", errors.getvalue())
        with patch.object(app, "pane_binding", side_effect=app.ContextMismatch("gone")), \
             patch("sys.stderr", new_callable=io.StringIO) as errors:
            server.refresh_sender_title(self.own_env, "Alice")
            self.assertIn("pane name unchanged", errors.getvalue())

    def test_client_forwards_only_pane_addressing_and_keeps_speech_unchanged(self):
        message = "Ready."
        info = {"prefix_policy": app.PREFIX_POLICY, "name_identity": True}
        with patch.dict(os.environ, {**self.own_env, "ELEVENLABS_API_KEY": "private"}), \
             patch.object(app, "endpoint", return_value=info), \
             patch.object(app, "rpc", new_callable=AsyncMock) as rpc:
            asyncio.run(app.send(self.paths, "Alice", EPOCH, message, message, {"pane_id": "own"}))
            payload = rpc.call_args.kwargs
            self.assertEqual((payload["name"], payload["epoch"]), ("Alice", EPOCH))
            self.assertEqual(payload["msg"], message)
            self.assertEqual(payload["tts"], message)
            self.assertNotIn("shell_pid", payload)
            self.assertLessEqual(set(payload["pane_env"]), app.PANE_ENV_KEYS)
            self.assertEqual(payload["pane_env"]["TUIOS_PANE_ID"], "own")
            self.assertNotIn("ELEVENLABS_API_KEY", payload["pane_env"])
