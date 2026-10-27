"""Pocket TTS routing and CLI behavior without downloads or live audio."""
import asyncio
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch
import wave

import agent_comms as app
import speech
from test_identity_groups import SOURCE
from test_agent_comms import Fixture


class PocketSynthesisTests(unittest.IsolatedAsyncioTestCase):
    async def test_cli_uses_stdin_and_measures_pcm(self):
        process = Mock(returncode=0, communicate=AsyncMock(return_value=(None, None)))
        with tempfile.TemporaryDirectory() as directory:
            async def launch(*args, **kwargs):
                self.assertEqual(args[:2], ("/tools/pocket-tts", "generate"))
                self.assertEqual(args[args.index("--voice") + 1], "alba")
                self.assertEqual(args[args.index("--language") + 1], "english")
                self.assertEqual(args[-2:], ("--text", "-"))
                self.assertNotIn("private text", args)
                self.assertEqual(kwargs["stdin"], asyncio.subprocess.PIPE)
                with wave.open(args[args.index("--output-path") + 1], "wb") as output:
                    output.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
                    output.writeframes(b"\0\0" * 24000)
                return process

            with patch.object(speech.shutil, "which", return_value="/tools/pocket-tts"), \
                    patch.object(speech.asyncio, "create_subprocess_exec", side_effect=launch):
                audio = await speech.synthesize_pocket_tts(directory, "alba", 'private<break time=".3s"/> text')
                self.assertEqual(audio.duration, 1)
                process.communicate.assert_awaited_once_with(input=b"private  text")
                audio.remove()
                self.assertFalse(list(Path(directory).iterdir()))

    async def test_missing_runtime_does_not_call_cloud(self):
        with patch.object(speech.shutil, "which", return_value=None), patch.object(speech, "synthesize") as cloud:
            with self.assertRaisesRegex(speech.SpeechError, "requires pocket-tts"):
                await speech.synthesize_pocket_tts("unused", "alba", "Hello")
            cloud.assert_not_called()

    async def test_timeout_and_cancel_kill_writer_and_remove_partial_output(self):
        for error in (asyncio.TimeoutError(), asyncio.CancelledError()):
            with self.subTest(error=error), tempfile.TemporaryDirectory() as directory:
                process = Mock(returncode=None, communicate=AsyncMock(side_effect=error), wait=AsyncMock())
                with patch.object(speech.shutil, "which", return_value="pocket-tts"), \
                        patch.object(speech.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)):
                    with self.assertRaises(type(error)):
                        await speech.synthesize_pocket_tts(directory, "alba", "Hello")
                process.kill.assert_called_once()
                self.assertFalse(list(Path(directory).iterdir()))

    async def test_watcher_routes_pocket_speed_and_normalizes_without_api_key(self):
        message = {"uid": "m", "phase": "preparing", "pocket_tts_voice": "alba", "tts": "Hello",
                   "pocket_tts_speed": 1.1, "phonon_speed": 0.7}
        audio, normalized = Mock(duration=1), Mock(duration=2)
        calls = []

        async def rpc(paths, command, **kwargs):
            calls.append((command, kwargs))
            if command == "claim-speech":
                return {"claimed": True, "lease": "test"}
            message.update(phase="displaying", started_at=0)

        with patch.dict(app.os.environ, {"ELEVENLABS_API_KEY": ""}), \
                patch.object(app, "rpc", side_effect=rpc), patch.object(app, "AudioWorkspace"), \
                patch.object(speech, "player", return_value="fake"), \
                patch.object(speech, "synthesize") as cloud, \
                patch.object(speech, "synthesize_phonon") as phonon, \
                patch.object(speech, "synthesize_pocket_tts", AsyncMock(return_value=audio)) as local, \
                patch.object(speech, "normalize", AsyncMock(return_value=normalized)), \
                patch.object(speech, "play", AsyncMock()) as play:
            await app.watcher_speech(None, {"message": message}, {"mode": "pocket_tts"}, False)
            self.assertEqual(local.call_args.args[1:], ("alba", "Hello", {}, 1.1))
            cloud.assert_not_called()
            phonon.assert_not_called()
            play.assert_awaited_once_with(normalized, "fake")
        self.assertEqual(calls[-1][1]["duration"], 2)
        audio.remove.assert_called_once()
        normalized.remove.assert_called_once()


class PocketReviewTests(Fixture):
    def test_local_voice_paths_resolve_from_config_not_working_directory(self):
        for voice in ("voices/my-voice.wav", "voices/my-voice.safetensors",
                      "./voices/voice with spaces.wav", "voice.wav", "voice.safetensors"):
            with self.subTest(voice=voice):
                source = ('[identities.a]\nname="Alice"\npocket_tts_voice_approved=true\n'
                          f'pocket_tts_voice={json.dumps(voice)}\n')
                self.paths.config.write_text(source)
                config = app.load_config(self.paths.config)
                expected = str(self.paths.config.resolve().parent / voice)
                self.assertEqual(config["identities"]["a"]["pocket_tts_voice"], expected)
                self.assertEqual(app.validate_message(config, "a", "Hello")["pocket_tts_voice"], expected)
                self.assertEqual(self.paths.config.read_text(), source)

    def test_presets_urls_absolute_and_home_paths(self):
        for voice, expected in (
                ("alba", "alba"), ("", ""),
                ("https://example.com/voice.wav", "https://example.com/voice.wav"),
                ("hf://voices/voice.safetensors", "hf://voices/voice.safetensors"),
                (str(self.paths.config.parent / "voice.wav"), str(self.paths.config.parent / "voice.wav")),
                ("~/voices/voice.wav", str(Path.home() / "voices/voice.wav"))):
            with self.subTest(voice=voice):
                self.paths.config.write_text(
                    f'[identities.a]\nname="Alice"\npocket_tts_voice={json.dumps(voice)}\n')
                self.assertEqual(app.load_config(self.paths.config)["identities"]["a"]["pocket_tts_voice"], expected)

    def test_grouped_local_voice_is_resolved_for_review(self):
        self.paths.config.write_text(
            '[speech]\nmode="pocket_tts"\n'
            '[identity_groups.pocket.identities.a]\nname="Alice"\n'
            'pocket_tts_voice="voices/alice.safetensors"\n')
        config = app.load_config(self.paths.config)
        expected = str(self.paths.config.resolve().parent / "voices/alice.safetensors")
        self.assertEqual(config["identity_groups"]["pocket"]["identities"]["a"]["pocket_tts_voice"], expected)
        with patch.object(app.sys.stdin, "isatty", return_value=True), \
                patch.object(speech, "player", return_value="fake"), \
                patch.object(speech, "synthesize_pocket_tts", AsyncMock(return_value=Mock())) as local, \
                patch.object(speech, "play", AsyncMock()), \
                patch("builtins.input", return_value="skip"), \
                patch("sys.stdout", new_callable=io.StringIO):
            app.voice_review(self.paths, config, all_voices=True)
        self.assertEqual(local.call_args.args[1], expected)

    def test_reviews_only_active_group_and_saves_provider_specific_decisions(self):
        self.paths.config.write_text(SOURCE)
        audio = Mock()
        with patch.object(app.sys.stdin, "isatty", return_value=True), \
                patch.dict(app.os.environ, {"ELEVENLABS_API_KEY": ""}), \
                patch.object(speech, "player", return_value="fake"), \
                patch.object(speech, "synthesize") as cloud, \
                patch.object(speech, "synthesize_pocket_tts", AsyncMock(return_value=audio)) as local, \
                patch.object(speech, "play", AsyncMock()), \
                patch("builtins.input", side_effect=["r", "yes", "skip"]), \
                patch("sys.stdout", new_callable=io.StringIO):
            app.voice_review(self.paths, app.load_config(self.paths.config), all_voices=True)
        self.assertEqual([c.args[1] for c in local.call_args_list], ["alba", "anna"])
        cloud.assert_not_called()
        config = app.load_config(self.paths.config)
        self.assertTrue(config["identities"]["c"]["pocket_tts_voice_approved"])
        self.assertNotIn("pocket_tts_voice_approved", config["identities"]["d"])
        self.assertIn("local Pocket TTS", app.communication_instructions(config))
        self.assertNotIn("<break", app.communication_instructions(config))
        self.assertEqual(app.validate_message(config, "c", "Hello")["pocket_tts_voice"], "alba")

    def test_invalid_pocket_settings_are_rejected(self):
        for settings in ('[speech.pocket_tts]\nexecutable=42\n',
                         '[speech.pocket_tts]\nlanguage=""\n',
                         '[speech]\npocket_tts=[]\n',
                         '[speech]\npocket_tts_speed=1.3\n'):
            with self.subTest(settings=settings), self.assertRaises(app.CommsError):
                self.paths.config.write_text(settings + '[identities.a]\nname="Alice"\n')
                app.load_config(self.paths.config)
