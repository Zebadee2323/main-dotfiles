"""Explicit watcher reloads remain independent from listener service restarts."""
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
    def test_restart_watchers_does_not_manage_service_or_bind_identity(self):
        with patch.object(app.Paths, "resolve", return_value=self.paths), \
                patch.object(app.service_manager, "manage") as manage, \
                patch.object(app, "pane_binding") as binding, \
                patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(app.main(["restart", "watchers"]), 0)
        self.assertTrue(app.watcher_revision(self.paths))
        manage.assert_not_called()
        binding.assert_not_called()
        self.assertFalse(self.paths.state.exists())

    def test_restart_all_signals_only_after_successful_service_restart(self):
        for argv in (["restart"], ["restart", "all"]):
            with self.subTest(argv=argv), \
                    patch.object(app.Paths, "resolve", return_value=self.paths), \
                    patch.object(app.service_manager, "manage") as manage, \
                    patch.object(app, "restart_watchers") as reload_watchers, \
                    patch("sys.stdout", new_callable=io.StringIO):
                manage.side_effect = lambda args, *a, **kw: (
                    self.assertEqual(args.action, "restart"), reload_watchers.assert_not_called())
                self.assertEqual(app.main(argv), 0)
                reload_watchers.assert_called_once_with(self.paths)

    def test_failed_service_restart_does_not_signal_watchers(self):
        with patch.object(app.Paths, "resolve", return_value=self.paths), \
                patch.object(app.service_manager, "manage", side_effect=app.service_manager.ServiceError("failed")), \
                patch.object(app, "restart_watchers") as reload_watchers, \
                patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(app.main(["restart", "all"]), 1)
        reload_watchers.assert_not_called()

    def test_service_only_does_not_signal_watchers(self):
        with patch.object(app.Paths, "resolve", return_value=self.paths), \
                patch.object(app.service_manager, "manage", return_value=None), \
                patch.object(app, "restart_watchers") as reload_watchers:
            self.assertEqual(app.main(["service", "restart"]), 0)
        reload_watchers.assert_not_called()

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
    async def test_watcher_renders_loaded_mode_including_muted_badge(self):
        for mode, badge in (("elevenlabs", "[ELE]"), ("phonon", "[PHO]"), ("pocket_tts", "[POC]")):
            with self.subTest(mode=mode), \
                    patch.object(app, "connect", AsyncMock(return_value=(Mock(), Mock(), snapshot("one")))), \
                    patch.object(app, "read_frame", AsyncMock(side_effect=asyncio.CancelledError())), \
                    patch.object(app, "close_writer", AsyncMock()), \
                    patch("sys.stdout", new_callable=io.StringIO) as output:
                with self.assertRaises(asyncio.CancelledError):
                    await app.watch_stream(self.paths, color=False, width=80, muted=True, settings={"mode": mode})
                self.assertIn(f"[Muted] {badge} comms: idle", output.getvalue())

    async def test_real_offline_watcher_reexecutes_on_explicit_signal(self):
        process = await asyncio.create_subprocess_exec(
            sys.executable, str(Path(app.__file__).resolve()), "watch", "--mute", "--no-color",
            "--config", str(self.paths.config), "--runtime-dir", str(self.paths.runtime),
            "--state-dir", str(self.paths.state), stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)
        try:
            first = await asyncio.wait_for(process.stdout.readline(), 5)
            self.assertTrue(first, "watcher should display offline state")
            app.restart_watchers(self.paths)
            second = await asyncio.wait_for(process.stdout.readline(), 5)
            self.assertEqual(second, first)
            self.assertIsNone(process.returncode)
        finally:
            if process.returncode is None:
                process.terminate()
            await asyncio.wait_for(process.communicate(), 5)

    async def test_changed_listener_generation_does_not_reload(self):
        writer = Mock()
        with patch.object(app, "connect", AsyncMock(return_value=(Mock(), writer, snapshot("one")))), \
                patch.object(app, "read_frame", AsyncMock(side_effect=[snapshot("two"), asyncio.CancelledError()])), \
                patch.object(app, "close_writer", AsyncMock()) as close, \
                patch("sys.stdout", new_callable=io.StringIO):
            with self.assertRaises(asyncio.CancelledError):
                await app.watch_stream(self.paths, color=False, width=80)
        close.assert_awaited_once_with(writer)

    async def test_disconnect_reconnect_to_new_listener_does_not_reload(self):
        writers = [Mock(), Mock()]
        with patch.object(app, "connect", AsyncMock(side_effect=[
                (Mock(), writers[0], snapshot("one")), (Mock(), writers[1], snapshot("two"))])) as connect, \
                patch.object(app, "read_frame", AsyncMock(side_effect=[app.CommsError("Disconnected"), asyncio.CancelledError()])), \
                patch.object(app, "close_writer", AsyncMock()) as close, \
                patch.object(app.asyncio, "sleep", AsyncMock()), \
                patch("sys.stdout", new_callable=io.StringIO):
            with self.assertRaises(asyncio.CancelledError):
                await app.watch_stream(self.paths, color=False, width=80)
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

    async def test_explicit_reload_cancels_offline_stream_and_closes_worker(self):
        started = asyncio.Event()
        stopped = asyncio.Event()

        async def offline(*args, **kwargs):
            started.set()
            try:
                await asyncio.Future()
            finally:
                stopped.set()

        worker = Mock(close=AsyncMock())
        with patch.object(app.speech, "PocketWorker", return_value=worker), \
                patch.object(app, "watch_stream", offline):
            task = asyncio.create_task(app.watch(
                self.paths, color=False, width=80, settings={"mode": "pocket_tts"}))
            await started.wait()
            app.restart_watchers(self.paths)
            self.assertTrue(await asyncio.wait_for(task, 2))
        self.assertTrue(stopped.is_set())
        worker.close.assert_awaited_once()
