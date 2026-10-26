import asyncio
from pathlib import Path
import re
import shutil
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch
import wave

import agent_comms as app
import speech


class RobotVoiceTests(unittest.IsolatedAsyncioTestCase):
    def test_filter_matches_naia_exactly(self):
        source = (Path(__file__).resolve().parents[2] / "nvim/lua/features/voice/runtime.lua").read_text()
        block = source.split("local robot_voice_filter = table.concat({", 1)[1].split('}, ",")', 1)[0]
        block = re.sub(r"--[^\n]*", "", block)
        entries = re.findall(r'"[^"]*"(?:\s*\.\.\s*"[^"]*")*', block)
        expected = ",".join("".join(re.findall(r'"([^"]*)"', entry)) for entry in entries)
        self.assertEqual(speech.ROBOT_FILTER, expected)

    async def test_robotize_selects_exact_filter(self):
        audio = Mock()
        with patch.object(speech, "filter_audio", new_callable=AsyncMock) as process:
            self.assertIs(await speech.robotize(audio), process.return_value)
        process.assert_awaited_once_with(audio, speech.ROBOT_FILTER, "Robot voice filter")

    def test_config_rejects_non_boolean_toggle(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            for value in ('"true"', "1", "[]"):
                path.write_text(f'[speech]\nrobot_mode = {value}\n[identities.a]\nname = "Alice"\n')
                with self.assertRaisesRegex(app.CommsError, "speech.robot_mode must be a boolean"):
                    app.load_config(path)

    async def test_watcher_toggles_order_fallback_and_cleanup(self):
        for robot, normalize, failure in (
            (False, False, None), (True, False, None), (False, True, None),
            (True, True, None), (True, True, "robot"), (True, True, "normalize"),
        ):
            with self.subTest(robot=robot, normalize=normalize, failure=failure), \
                 tempfile.TemporaryDirectory() as directory:
                originals = []
                calls = []

                def audio(name, duration):
                    result = speech.Audio(Path(directory) / name, duration)
                    result.path.write_bytes(b"test")
                    originals.append(result)
                    return result

                original = audio("input.wav", 1)
                current = original

                async def effect(source, name):
                    nonlocal current
                    calls.append(name)
                    self.assertIs(source, current)
                    self.assertTrue(source.path.exists())
                    if failure == name:
                        raise speech.SpeechError("test failure")
                    current = audio(name + ".wav", source.duration + 0.1)
                    return current

                message = {"uid": "m", "voice_id": "voice", "tts": "hello", "phase": "preparing"}
                state = {"message": message, "changed": asyncio.Event()}

                async def rpc(paths, command, **kwargs):
                    if command == "claim-speech":
                        return {"claimed": True, "lease": "lease"}
                    calls.append("ready")
                    self.assertEqual(kwargs["duration"], current.duration)
                    message.update(phase="displaying", started_at=0)

                async def robotize(source):
                    return await effect(source, "robot")

                async def normalizer(source):
                    return await effect(source, "normalize")

                with patch.dict("os.environ", {"ELEVENLABS_API_KEY": "fake"}), \
                     patch.object(speech, "player", return_value="fake"), \
                     patch.object(speech, "synthesize", return_value=original), \
                     patch.object(speech, "robotize", side_effect=robotize), \
                     patch.object(speech, "normalize", side_effect=normalizer), \
                     patch.object(speech, "play", new_callable=AsyncMock) as play, \
                     patch.object(app, "rpc", side_effect=rpc):
                    await app.watcher_speech(Mock(state=Path(directory)), state,
                                             {"robot_mode": robot, "normalize": normalize}, False)
                self.assertEqual(calls, (["robot"] if robot else []) +
                                 (["normalize"] if normalize else []) + ["ready"])
                play.assert_awaited_once_with(current, "fake")
                self.assertTrue(all(not item.path.exists() for item in originals))

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg not installed")
    async def test_real_ffmpeg_robot_graph_and_normalization(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.wav"
            with wave.open(str(source), "wb") as stream:
                stream.setparams((1, 2, speech.SAMPLE_RATE, 0, "NONE", "not compressed"))
                stream.writeframes(b"\x00\x10\x00\xf0" * (speech.SAMPLE_RATE // 2))
            processed = await speech.robotize(speech.Audio(source, 1))
            self.assertGreaterEqual(processed.duration, 1)
            normalized = await speech.normalize(processed)
            self.assertAlmostEqual(normalized.duration, processed.duration, places=3)
            with wave.open(str(normalized.path), "rb") as stream:
                self.assertEqual(stream.getframerate(), speech.SAMPLE_RATE)
                self.assertEqual(stream.getsampwidth(), 2)
                self.assertEqual(stream.getnchannels(), 1)
