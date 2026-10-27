"""Listener restarts reload watcher code/config without PID hunting or identity resets."""
import asyncio
import io
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import agent_comms as app
from test_agent_comms import Fixture


def snapshot(generation):
    return {"ok": True, "event": "state", "message": None, "audio": {},
            "listener_generation": generation}


class WatcherRestartTests(Fixture):
    def test_each_listener_has_a_public_generation_separate_from_auth(self):
        first = app.Server(self.paths, app.load_config(self.paths.config), quiet=True)
        second = app.Server(self.paths, app.load_config(self.paths.config), quiet=True)
        self.assertNotEqual(first.generation, second.generation)
        self.assertNotEqual(first.generation, first.token)
        self.assertEqual(first.snapshot()["listener_generation"], first.generation)
        self.assertNotIn(first.token, str(first.snapshot()))

    def test_reexec_preserves_cli_arguments_without_allocating_or_clearing_identity(self):
        argv = ["watch", "--width", "80", "--mute", "--no-vfx", "--no-flash", "--config", str(self.paths.config)]
        with patch.object(app.Paths, "resolve", return_value=self.paths), \
                patch.object(app, "watch", AsyncMock(return_value=True)), \
                patch.object(app.os, "execv") as execute, \
                patch.object(app, "pane_binding") as binding:
            self.assertEqual(app.main(argv), 0)
        execute.assert_called_once_with(sys.executable, [sys.executable, str(Path(app.__file__).resolve()), *argv])
        binding.assert_not_called()
        self.assertFalse(self.paths.state.exists())


class AsyncWatcherRestartTests(Fixture, unittest.IsolatedAsyncioTestCase):
    async def test_changed_generation_returns_reload_even_without_disconnect(self):
        writer = Mock()
        with patch.object(app, "connect", AsyncMock(return_value=(Mock(), writer, snapshot("one")))), \
                patch.object(app, "read_frame", AsyncMock(return_value=snapshot("two"))), \
                patch.object(app, "close_writer", AsyncMock()) as close, \
                patch("sys.stdout", new_callable=io.StringIO):
            self.assertTrue(await app.watch_stream(self.paths, color=False, width=80))
        close.assert_awaited_once_with(writer)

    async def test_disconnect_reconnect_to_new_listener_reloads_watcher(self):
        writers = [Mock(), Mock()]
        with patch.object(app, "connect", AsyncMock(side_effect=[
                (Mock(), writers[0], snapshot("one")), (Mock(), writers[1], snapshot("two"))])) as connect, \
                patch.object(app, "read_frame", AsyncMock(side_effect=app.CommsError("Disconnected"))), \
                patch.object(app, "close_writer", AsyncMock()) as close, \
                patch.object(app.asyncio, "sleep", AsyncMock()), \
                patch("sys.stdout", new_callable=io.StringIO):
            self.assertTrue(await app.watch_stream(self.paths, color=False, width=80))
        self.assertEqual(connect.await_count, 2)
        self.assertEqual(close.await_count, 2)

    async def test_same_generation_and_legacy_frames_do_not_restart(self):
        calls = [snapshot("one"), snapshot("one"), snapshot(None)]
        with patch.object(app, "connect", AsyncMock(return_value=(Mock(), Mock(), calls[0]))), \
                patch.object(app, "read_frame", AsyncMock(side_effect=[calls[1], calls[2], asyncio.CancelledError()])), \
                patch.object(app, "close_writer", AsyncMock()), \
                patch("sys.stdout", new_callable=io.StringIO):
            with self.assertRaises(asyncio.CancelledError):
                await app.watch_stream(self.paths, color=False, width=80)

    async def test_reload_closes_resident_worker_before_reexec(self):
        worker = Mock(close=AsyncMock())
        with patch.object(app.speech, "PocketWorker", return_value=worker), \
                patch.object(app, "watch_stream", AsyncMock(return_value=True)):
            self.assertTrue(await app.watch(self.paths, color=False, width=80, settings={"mode": "pocket_tts"}))
        worker.close.assert_awaited_once()
