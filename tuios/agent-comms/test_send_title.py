import asyncio
import io
import json
import os
from unittest.mock import AsyncMock, Mock, patch

import agent_comms as app
from test_agent_comms import Fixture


class SendTitleTests(Fixture):
    pane_env = {"TUIOS_ENV": "1", "TUIOS_PANE_ID": "own", "TUIOS_SESSION": "work"}

    def server(self):
        return app.Server(self.paths, app.load_config(self.paths.config), quiet=True)

    async def submit(self, server, **fields):
        reader = asyncio.StreamReader()
        reader.feed_data(json.dumps({"protocol_version": app.VERSION, "token": server.token,
            "command": "send", "id": "a", "msg": "Ready.", "pane_env": self.pane_env, **fields}).encode() + b"\n")
        reader.feed_eof()
        writer = Mock(drain=AsyncMock(), wait_closed=AsyncMock())
        await asyncio.wait_for(server.handle(reader, writer), 1)
        return json.loads(writer.write.call_args_list[0].args[0])

    def test_send_acknowledges_without_waiting_for_title_and_coalesces_per_pane(self):
        async def exercise():
            server = self.server()
            release = asyncio.Event()

            async def slow_thread(*args):
                await release.wait()

            with patch.object(app.asyncio, "to_thread", side_effect=slow_thread) as worker:
                try:
                    self.assertTrue((await self.submit(server))["ok"])
                    self.assertTrue((await self.submit(server))["ok"])
                    self.assertEqual(server.queue.qsize(), 2)
                    self.assertEqual(len(server.title_refreshes), 1)
                    self.assertEqual(worker.call_count, 1)
                finally:
                    release.set()
                    await asyncio.gather(*server.title_refreshes.values())
                await asyncio.sleep(0)
                self.assertEqual(server.title_refreshes, {})
        asyncio.run(exercise())

    def test_rejected_send_does_not_refresh_and_invalid_optional_environment_is_nonfatal(self):
        async def exercise():
            server = self.server()
            with patch.object(app.asyncio, "to_thread") as worker:
                self.assertFalse((await self.submit(server, msg=""))["ok"])
                self.assertTrue((await self.submit(server, pane_env=None))["ok"])
                self.assertTrue((await self.submit(server, pane_env={**self.pane_env, "PATH": "/bad"}))["ok"])
                worker.assert_not_called()
                self.assertEqual(server.title_refreshes, {})
        asyncio.run(exercise())

    def test_listener_rechecks_binding_and_live_context_without_allocating(self):
        server = self.server()
        source = {"pane_id": "own", "pane_title": "New task"}
        with patch.object(app, "pane_binding", return_value="a") as binding, \
             patch.object(app, "checked_context", return_value=source) as context, \
             patch.object(app, "update_pane_title") as update:
            server.refresh_sender_title(self.pane_env, "a")
            binding.assert_called_once_with(self.paths, server.config, allocate=False, env=self.pane_env)
            context.assert_called_once_with(self.pane_env)
            update.assert_called_once_with(self.paths, server.config, "a", source, self.pane_env)
            update.reset_mock()
            context.reset_mock()
            binding.return_value = "b"
            server.refresh_sender_title(self.pane_env, "a")
            context.assert_not_called()
            update.assert_not_called()

    def test_listener_naming_failures_are_best_effort(self):
        server = self.server()
        with patch.object(app, "pane_binding", return_value="a"), \
             patch.object(app, "checked_context", side_effect=PermissionError("denied")), \
             patch("sys.stderr", new_callable=io.StringIO) as errors:
            server.refresh_sender_title(self.pane_env, "a")
            self.assertIn("pane name unchanged", errors.getvalue())

    def test_client_forwards_only_pane_addressing_and_keeps_speech_unchanged(self):
        message = "Ready."
        with patch.dict(os.environ, {**self.pane_env, "ELEVENLABS_API_KEY": "private"}), \
             patch.object(app, "endpoint", return_value={"prefix_policy": app.PREFIX_POLICY}), \
             patch.object(app, "rpc", new_callable=AsyncMock) as rpc:
            asyncio.run(app.send(self.paths, "a", message, message, {"pane_id": "own"}))
            payload = rpc.call_args.kwargs
            self.assertEqual(payload["msg"], message)
            self.assertEqual(payload["tts"], message)
            self.assertLessEqual(set(payload["pane_env"]), app.PANE_ENV_KEYS)
            self.assertEqual(payload["pane_env"]["TUIOS_PANE_ID"], "own")
            self.assertNotIn("ELEVENLABS_API_KEY", payload["pane_env"])
