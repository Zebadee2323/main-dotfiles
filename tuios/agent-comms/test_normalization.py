import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch
import wave

import agent_comms as app
import speech


class NormalizationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.audio = speech.Audio(Path(self.temp.name) / "original.wav", 1)
        self.audio.path.write_bytes(b"original")

    async def test_normalizes_to_pcm_and_measures_output(self):
        async def launch(*args, **kwargs):
            self.assertEqual(args[args.index("-af") + 1], speech.NORMALIZE_FILTER)
            self.assertEqual(args[args.index("-c:a") + 1], "pcm_s16le")
            with wave.open(args[-1], "wb") as stream:
                stream.setnchannels(1)
                stream.setsampwidth(2)
                stream.setframerate(speech.SAMPLE_RATE)
                stream.writeframes(b"\0\0" * (speech.SAMPLE_RATE // 2))
            return Mock(returncode=0, wait=AsyncMock(return_value=0))

        with patch.object(speech.shutil, "which", return_value="ffmpeg"), \
             patch.object(speech.asyncio, "create_subprocess_exec", side_effect=launch):
            normalized = await speech.normalize(self.audio)
        self.assertEqual(normalized.duration, 0.5)
        self.assertEqual(self.audio.path.read_bytes(), b"original")
        normalized.remove()
        self.assertEqual(list(Path(self.temp.name).iterdir()), [self.audio.path])

    async def test_missing_ffmpeg_creates_no_output(self):
        with patch.object(speech.shutil, "which", return_value=None):
            with self.assertRaises(speech.SpeechError):
                await speech.normalize(self.audio)
        self.assertEqual(list(Path(self.temp.name).iterdir()), [self.audio.path])

    async def test_failure_and_invalid_output_remove_partial_file(self):
        for code in (1, 0):
            process = Mock(returncode=code, wait=AsyncMock(return_value=code))
            with patch.object(speech.shutil, "which", return_value="ffmpeg"), \
                 patch.object(speech.asyncio, "create_subprocess_exec", return_value=process):
                with self.assertRaises(speech.SpeechError):
                    await speech.normalize(self.audio)
            self.assertEqual(list(Path(self.temp.name).iterdir()), [self.audio.path])

    async def test_cancellation_kills_ffmpeg_and_removes_output(self):
        started = asyncio.Event()

        async def wait():
            started.set()
            await asyncio.Event().wait()

        process = Mock(returncode=None)
        # Use a coroutine function so the first wait is actually cancellable.
        async def process_wait():
            if process.returncode is None:
                await wait()
            return 0

        process.wait = AsyncMock(side_effect=process_wait)
        process.kill.side_effect = lambda: setattr(process, "returncode", -9)
        with patch.object(speech.shutil, "which", return_value="ffmpeg"), \
             patch.object(speech.asyncio, "create_subprocess_exec", return_value=process):
            task = asyncio.create_task(speech.normalize(self.audio))
            await asyncio.wait_for(started.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        process.kill.assert_called_once()
        self.assertEqual(list(Path(self.temp.name).iterdir()), [self.audio.path])

    async def test_watcher_normalizes_before_ready_and_falls_back_or_opts_out(self):
        for enabled, fails in ((True, False), (True, True), (False, False)):
            original = speech.Audio(self.audio.path, 1)
            normalized = speech.Audio(Path(self.temp.name) / "normalized.wav", 0.5)
            self.audio.path.write_bytes(b"original")
            normalized.path.write_bytes(b"normalized")
            message = {"uid": "m", "voice_id": "voice", "tts": "hello", "phase": "preparing"}
            state = {"message": message, "changed": asyncio.Event()}

            async def rpc(paths, command, **kwargs):
                if command == "claim-speech":
                    return {"claimed": True, "lease": "lease"}
                self.assertEqual(kwargs["duration"], 0.5 if enabled and not fails else 1)
                message.update(phase="displaying", started_at=0)

            with patch.dict("os.environ", {"ELEVENLABS_API_KEY": "fake"}), \
                 patch.object(speech, "player", return_value="fake"), \
                 patch.object(speech, "synthesize", return_value=original), \
                 patch.object(speech, "normalize", new_callable=AsyncMock) as normalize, \
                 patch.object(speech, "play", new_callable=AsyncMock) as play, \
                 patch.object(app, "rpc", side_effect=rpc):
                normalize.return_value = normalized
                if fails:
                    normalize.side_effect = speech.SpeechError("failed")
                await app.watcher_speech(Mock(state=Path(self.temp.name)), state, {"normalize": enabled}, False)
            self.assertEqual(normalize.await_count, int(enabled))
            self.assertEqual(play.await_args.args[0], normalized if enabled and not fails else original)
            self.assertFalse(original.path.exists())
            if enabled and not fails:
                self.assertFalse(normalized.path.exists())
