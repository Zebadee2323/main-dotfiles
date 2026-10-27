"""Resident-model lifetime, demand-only voice caches, and safe cancellation."""
import asyncio
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import agent_comms as app
import pocket_worker
import speech


class VoiceCacheTests(unittest.TestCase):
    def test_model_loads_once_and_only_used_voices_are_cached(self):
        model = Mock(config=SimpleNamespace(mimi=SimpleNamespace(sample_rate=24000)))
        model.get_state_for_audio_prompt.side_effect = lambda name: {"voice": name}
        loader, writer = Mock(return_value=model), Mock()
        worker = pocket_worker.PocketSynthesizer("english", loader=loader, writer=writer)
        loader.assert_not_called()
        self.assertEqual(worker.voices, {})
        results = [worker.synthesize(voice, "Hello", "out.wav") for voice in ("mary", "alba", "mary")]
        loader.assert_called_once_with("english")
        self.assertEqual([c.args[0] for c in model.get_state_for_audio_prompt.call_args_list], ["mary", "alba"])
        self.assertEqual(set(worker.voices), {"mary", "alba"})
        self.assertEqual([r["voice_cache_hit"] for r in results], [False, False, True])
        self.assertEqual([r["cached_voices"] for r in results], [1, 2, 2])
        self.assertTrue(all(c.kwargs["copy_state"] for c in model.generate_audio_stream.call_args_list))
        self.assertEqual(writer.call_count, 3)

    def test_failed_voice_loading_does_not_cache_a_partial_state(self):
        model = Mock()
        model.get_state_for_audio_prompt.side_effect = ValueError("bad voice")
        worker = pocket_worker.PocketSynthesizer("english", loader=Mock(return_value=model))
        with self.assertRaises(ValueError):
            worker.synthesize("bad", "Hello", "out.wav")
        self.assertEqual(worker.voices, {})

    def test_protocol_sanitizes_errors_and_rejects_invalid_input(self):
        source = io.StringIO('null\n{"voice":"mary","text":"Hello","path":"out.wav"}\n')
        output = io.StringIO()
        with patch.object(pocket_worker, "PocketSynthesizer") as worker:
            worker.return_value.synthesize.side_effect = ValueError("private diagnostic")
            self.assertEqual(pocket_worker.serve(source, output, "english"), 0)
        self.assertEqual(output.getvalue(), '{"ok": false}\n{"ok": false}\n')
        worker.return_value.synthesize.assert_called_once()

    def test_protocol_keeps_library_stdout_out_of_responses(self):
        output, errors = io.StringIO(), io.StringIO()

        def synthesize(*args):
            print("library chatter")
            return {"ok": True}

        with patch.object(pocket_worker, "PocketSynthesizer") as worker, patch("sys.stderr", errors):
            worker.return_value.synthesize.side_effect = synthesize
            pocket_worker.serve(io.StringIO('{"voice":"mary","text":"Hello","path":"out.wav"}\n'), output, "english")
        self.assertEqual(output.getvalue(), '{"ok": true}\n')
        self.assertEqual(errors.getvalue(), "library chatter\n")


FAKE_WORKER = '''import json,sys,time,wave
voices=set()
for line in sys.stdin:
 r=json.loads(line)
 if r["text"]=="exit": sys.exit(1)
 if r["text"]=="error":
  print(json.dumps({"ok":False}),flush=True); continue
 if r["text"]=="invalid":
  print(json.dumps({"ok":True}),flush=True); continue
 hit=r["voice"] in voices
 voices.add(r["voice"])
 with wave.open(r["path"],"wb") as w:
  w.setparams((1,2,24000,0,"NONE","not compressed"))
  w.writeframes(b"\\0\\0"*24000)
 if r["text"]=="hang": time.sleep(60)
 print(json.dumps({"ok":True,"voice_cache_hit":hit,"cached_voices":len(voices)}),flush=True)
'''


class WorkerProcessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.output = self.root / "audio"
        self.output.mkdir()
        script = self.root / "fake_worker.py"
        script.write_text(FAKE_WORKER)
        launch = asyncio.create_subprocess_exec
        self.processes = []

        async def spawn(*args, **kwargs):
            self.assertTrue(str(args[2]).endswith("pocket_worker.py"))
            process = await launch(sys.executable, "-u", str(script), **kwargs)
            self.processes.append(process)
            return process

        self.spawn = patch.object(speech.asyncio, "create_subprocess_exec", side_effect=spawn)
        self.spawn.start()
        self.addCleanup(self.spawn.stop)
        interpreter = patch.object(speech.PocketWorker, "interpreter", return_value=sys.executable)
        interpreter.start()
        self.addCleanup(interpreter.stop)
        self.worker = speech.PocketWorker()
        self.addAsyncCleanup(self.worker.close)

    async def test_one_process_reused_and_switching_back_hits_cache(self):
        self.assertIsNone(self.worker.process)
        for voice, hit, count in (("mary", False, 1), ("alba", False, 2), ("mary", True, 2)):
            audio = await self.worker.synthesize(self.output, voice, "Hello")
            self.assertEqual(audio.duration, 1)
            self.assertEqual(self.worker.last_stats, {"voice_cache_hit": hit, "cached_voices": count})
            audio.remove()
        self.assertEqual(len(self.processes), 1)
        await self.worker.close()
        self.assertIsNotNone(self.processes[0].returncode)
        self.assertFalse(list(self.output.iterdir()))

    async def test_concurrent_requests_are_serialized(self):
        audio = await asyncio.gather(*(self.worker.synthesize(self.output, v, "Hello") for v in ("mary", "alba", "mary")))
        for item in audio:
            item.remove()
        self.assertEqual(len(self.processes), 1)
        self.assertEqual(self.worker.last_stats, {"voice_cache_hit": True, "cached_voices": 2})

    async def test_timeout_kills_writer_cleans_audio_and_next_request_recovers(self):
        with patch.object(speech, "REQUEST_TIMEOUT", 0.3):
            with self.assertRaises(asyncio.TimeoutError):
                await self.worker.synthesize(self.output, "mary", "hang")
        self.assertIsNone(self.worker.process)
        self.assertIsNotNone(self.processes[0].returncode)
        self.assertFalse(list(self.output.iterdir()))
        audio = await self.worker.synthesize(self.output, "alba", "Hello")
        self.assertEqual(len(self.processes), 2)
        self.assertEqual(self.worker.last_stats["cached_voices"], 1)
        audio.remove()

    async def test_worker_failure_and_invalid_audio_clean_up(self):
        for text in ("error", "exit", "invalid"):
            with self.subTest(text=text):
                with self.assertRaises(speech.SpeechError):
                    await self.worker.synthesize(self.output, "mary", text)
                self.assertFalse(list(self.output.iterdir()))

    async def test_cancellation_closes_writer_before_removing_output(self):
        started = asyncio.Event()
        original = self.worker._request

        async def request(*args):
            task = asyncio.create_task(original(*args))
            # _request is active before cancellation; all subprocesses are test-owned.
            await asyncio.sleep(0.1)
            started.set()
            try:
                await task
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        with patch.object(self.worker, "_request", side_effect=request):
            task = asyncio.create_task(self.worker.synthesize(self.output, "mary", "hang"))
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertIsNone(self.worker.process)
        self.assertIsNotNone(self.processes[0].returncode)
        self.assertFalse(list(self.output.iterdir()))

    async def test_speed_filter_runs_after_generation_without_discarding_model(self):
        processed = Mock()
        with patch.object(speech, "filter_audio", AsyncMock(return_value=processed)) as filter_audio:
            self.assertIs(await self.worker.synthesize(self.output, "mary", "Hello", 1.2), processed)
        self.assertEqual(filter_audio.call_args.args[1], "atempo=1.2")
        self.assertIsNone(self.worker.process.returncode)
        self.assertFalse(list(self.output.iterdir()))


class WatcherWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_watcher_owns_worker_but_does_not_start_or_preload_it(self):
        worker = Mock(close=AsyncMock())
        with patch.object(speech, "PocketWorker", return_value=worker), \
                patch.object(app, "watch_stream", AsyncMock(side_effect=asyncio.CancelledError())) as stream:
            with self.assertRaises(asyncio.CancelledError):
                await app.watch(None, color=False, width=80, settings={"mode": "pocket_tts"})
        self.assertIs(stream.call_args.kwargs["pocket_worker"], worker)
        worker.synthesize.assert_not_called()
        worker.close.assert_awaited_once()

    async def test_non_pocket_watchers_do_not_create_a_pocket_worker(self):
        with patch.object(speech, "PocketWorker") as worker, patch.object(app, "watch_stream", AsyncMock()):
            await app.watch(None, color=False, width=80, settings={"mode": "phonon"})
        worker.assert_not_called()

    async def test_losing_a_lease_does_not_load_a_model_or_voice(self):
        worker = Mock(synthesize=AsyncMock())
        state = {"message": {"uid": "m", "phase": "preparing", "pocket_tts_voice": "mary"}, "pocket_worker": worker}
        with patch.object(app, "rpc", AsyncMock(return_value={"claimed": False})), \
                patch.object(speech, "player", return_value="fake"):
            await app.watcher_speech(None, state, {"mode": "pocket_tts"}, False)
        worker.synthesize.assert_not_called()

    async def test_lease_winner_uses_resident_worker_and_muted_messages_do_not_load_voices(self):
        message = {"uid": "m", "phase": "preparing", "pocket_tts_voice": "mary", "tts": "Hello", "pocket_tts_speed": 1.1}
        worker = Mock(synthesize=AsyncMock(return_value=Mock(duration=1)))
        state = {"message": message, "pocket_worker": worker}

        async def rpc(paths, command, **kwargs):
            if command == "claim-speech":
                return {"claimed": True, "lease": "test"}
            message.update(phase="displaying", started_at=0)

        with patch.object(app, "rpc", side_effect=rpc), patch.object(app, "AudioWorkspace"), \
                patch.object(speech, "player", return_value="fake"), \
                patch.object(speech, "synthesize_pocket_tts") as cli, \
                patch.object(speech, "play", AsyncMock()):
            settings = {"mode": "pocket_tts", "normalize": False}
            await app.watcher_speech(None, state, settings, True)
            worker.synthesize.assert_not_called()
            await app.watcher_speech(None, state, settings, False)
            self.assertEqual(worker.synthesize.call_args.args[1:], ("mary", "Hello", 1.1))
            cli.assert_not_called()
