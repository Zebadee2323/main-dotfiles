"""Speech gain, live playback changes, and transient dock feedback."""
import asyncio
import io
from pathlib import Path
import struct
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch
import wave

import agent_comms as app
import speech


class VoiceVolumeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.audio = speech.Audio(Path(self.temp.name) / "source.wav", 1)
        with wave.open(str(self.audio.path), "wb") as stream:
            stream.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
            stream.writeframes(struct.pack("<hh", 20000, -20000) * 12000)

    async def test_gain_and_seek_preserve_original_and_duration(self):
        original = self.audio.path.read_bytes()
        for level in (10, 25, 50, 100):
            excerpt = speech.playback_excerpt(self.audio, 0.25, level)
            with wave.open(str(excerpt.path), "rb") as stream:
                self.assertEqual(stream.getnframes(), 18000)
                self.assertEqual(struct.unpack("<hh", stream.readframes(2)),
                                 (200 * level, -200 * level))
            self.assertEqual(excerpt.duration, 0.75)
            excerpt.remove()
        self.assertEqual(self.audio.path.read_bytes(), original)

    async def test_live_change_stops_old_player_and_plays_only_remaining_audio(self):
        level = 100
        started, stopped, resumed = asyncio.Event(), asyncio.Event(), asyncio.Event()
        calls = []

        async def play(audio, executable):
            calls.append(audio)
            if len(calls) == 1:
                started.set()
                try:
                    await asyncio.Future()
                finally:
                    stopped.set()
            else:
                self.assertTrue(stopped.is_set())
                with wave.open(str(audio.path), "rb") as stream:
                    self.assertEqual(abs(struct.unpack("<h", stream.readframes(1))[0]), 5000)
                resumed.set()
                await asyncio.Future()

        with patch.object(speech, "play", side_effect=play):
            task = asyncio.create_task(speech.play_live(self.audio, "fake", lambda: level))
            try:
                await asyncio.wait_for(started.wait(), 1)
                level = 25
                await asyncio.wait_for(resumed.wait(), 1)
                self.assertLess(calls[1].duration, self.audio.duration)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(list(self.audio.path.parent.iterdir()), [self.audio.path])

    async def test_idle_volume_notice_expires_without_another_listener_event(self):
        response = {"event": "state", "message": None,
                    "audio": {"volume": 25, "volume_notice_until": time.monotonic() + 0.08}}
        output = io.StringIO()

        async def read(*args, **kwargs):
            await asyncio.Future()

        with patch.object(app, "connect", AsyncMock(return_value=(None, None, response))), \
             patch.object(app, "read_frame", side_effect=read), \
             patch("sys.stdout", output):
            task = asyncio.create_task(app.watch_stream(None, color=False, width=80,
                                                       settings={"mode": "phonon"}))
            try:
                await asyncio.sleep(0.15)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        lines = output.getvalue().splitlines()
        self.assertIn("[Volume 25%]", lines[0])
        self.assertNotIn("Volume", lines[-1])
        self.assertTrue(all(speech.width(line) <= 80 for line in lines))
