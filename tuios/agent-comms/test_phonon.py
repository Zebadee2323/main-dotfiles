"""Optional local synthesis: config, routing, cleanup, and no cloud fallback."""
import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch
import wave

import agent_comms as app
import speech


class ConfigTests(unittest.TestCase):
    def test_config_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            for settings, valid in (
                    ('', True), ('[speech]\nmode="phonon"\n', True),
                    ('[speech]\nmode="elevenlabs"\n', True),
                    ('[speech]\nmode="other"\n', False),
                    ('[speech]\nmode=true\n', False),
                    ('[speech]\nphonon=[]\n', False),
                    ('[speech.phonon]\nmodel=""\n', False),
                    ('[speech.phonon]\nexecutable=42\n', False)):
                with self.subTest(settings=settings):
                    path.write_text(settings + '[identities.a]\nname="Alice"\nphonon_voice="Juniper"\n')
                    if valid:
                        self.assertEqual(app.load_config(path)["identities"]["a"]["phonon_voice"], "Juniper")
                    else:
                        with self.assertRaises(app.CommsError):
                            app.load_config(path)
            path.write_text('[identities.a]\nname="Alice"\nphonon_voice=42\n')
            with self.assertRaisesRegex(app.CommsError, "phonon_voice must be text"):
                app.load_config(path)

    def test_message_preserves_both_providers(self):
        config = {"identities": {"a": {"name": "Alice", "phonon_voice": " Juniper ",
                                        "eleven_labs_voice_id": "cloud"}}}
        message = app.validate_message(config, "a", "Hello")
        self.assertEqual(message["phonon_voice"], "Juniper")
        self.assertEqual(message["voice_id"], "cloud")
        self.assertIn("TTS mode: `phonon`", app.communication_instructions({"speech": {"mode": "phonon"}}))
        self.assertNotIn("<break", app.communication_instructions({"speech": {"mode": "phonon"}}))
        self.assertNotIn("{speech_", app.communication_instructions(config))
        self.assertIn("eleven_turbo_v2_5", app.communication_instructions(config))

    def test_watcher_rejects_invalid_config_instead_of_cloud_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text('[speech]\nmode="typo"\n[identities.a]\nname="Alice"\n')
            with patch.object(app.Paths, "resolve", return_value=Mock(config=path)), \
                    patch.object(app, "watch") as watch, \
                    patch.object(app, "prepare_output"):
                self.assertEqual(app.main(["watch"]), 1)
                watch.assert_not_called()


class SynthesisTests(unittest.IsolatedAsyncioTestCase):
    async def test_cli_arguments_and_measured_duration(self):
        with tempfile.TemporaryDirectory() as directory:
            async def launch(*args, **kwargs):
                self.assertEqual(args[args.index("--model") + 1], speech.PHONON_MODEL)
                self.assertEqual(args[args.index("--voice") + 1], "Juniper")
                self.assertEqual(args[-2], "--")
                self.assertEqual(args[-1], "Hello  there")
                path = args[args.index("--output") + 1]
                with wave.open(path, "wb") as output:
                    output.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
                    output.writeframes(b"\0\0" * 24000)
                return Mock(wait=AsyncMock(return_value=0), returncode=0)

            with patch.object(speech.shutil, "which", return_value="/tools/ptts"), \
                    patch.object(speech.asyncio, "create_subprocess_exec", side_effect=launch):
                audio = await speech.synthesize_phonon(directory, "Juniper", 'Hello<break time="0.3s" /> there')
                self.assertEqual(audio.duration, 1)
                audio.remove()
                self.assertEqual(list(Path(directory).iterdir()), [])

    async def test_missing_runtime_never_calls_cloud(self):
        with patch.object(speech.shutil, "which", return_value=None), \
                patch.object(speech, "synthesize") as cloud:
            with self.assertRaisesRegex(speech.SpeechError, "requires ptts"):
                await speech.synthesize_phonon("unused", "Juniper", "Hello")
            cloud.assert_not_called()

    async def test_failure_invalid_wav_timeout_and_cancellation_clean_up(self):
        for outcome in (1, 0, asyncio.TimeoutError(), asyncio.CancelledError()):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as directory:
                process = Mock(returncode=None, wait=AsyncMock(side_effect=[outcome, 0]))
                with patch.object(speech.shutil, "which", return_value="ptts"), \
                        patch.object(speech.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)):
                    with self.assertRaises((speech.SpeechError, asyncio.TimeoutError, asyncio.CancelledError)):
                        await speech.synthesize_phonon(directory, "Juniper", "private text")
                self.assertEqual(list(Path(directory).iterdir()), [])
                process.kill.assert_called_once()


class WatcherTests(unittest.IsolatedAsyncioTestCase):
    async def test_phonon_needs_no_key_and_uses_identity_speed(self):
        message = {"uid": "m", "phase": "preparing", "phonon_voice": "Juniper",
                   "voice_id": "cloud", "tts": "Hello", "voice_speed": 1.2}

        async def rpc(paths, command, **kwargs):
            if command == "claim-speech":
                return {"claimed": True, "lease": "l"}
            message.update(phase="displaying", started_at=0)

        for failure in (False, True):
            with self.subTest(failure=failure):
                message["phase"] = "preparing"
                audio = Mock(duration=1)
                with patch.dict(app.os.environ, {"ELEVENLABS_API_KEY": ""}), \
                        patch.object(app, "rpc", side_effect=rpc), \
                        patch.object(app, "AudioWorkspace") as workspace, \
                        patch.object(speech, "player", return_value="fake"), \
                        patch.object(speech, "synthesize") as cloud, \
                        patch.object(speech, "synthesize_phonon", AsyncMock(
                            return_value=audio, side_effect=speech.SpeechError("failed") if failure else None)) as local, \
                        patch.object(speech, "play", AsyncMock()) as play:
                    await app.watcher_speech(None, {"message": message},
                                             {"mode": "phonon", "normalize": False, "speed": 0.9}, False)
                    self.assertEqual(local.call_args.args[1:], ("Juniper", "Hello", {}, 1.2))
                    cloud.assert_not_called()
                    self.assertEqual(play.await_count, 0 if failure else 1)
                    workspace.return_value.close.assert_called_once()
                    if not failure:
                        audio.remove.assert_called_once()

    async def test_mute_and_missing_voice_do_not_claim_or_synthesize(self):
        for muted, voice in ((True, "Juniper"), (False, "")):
            with patch.object(app, "rpc", AsyncMock()) as rpc, \
                    patch.object(speech, "player", return_value="fake"):
                await app.watcher_speech(None, {"message": {"phase": "preparing", "phonon_voice": voice}},
                                         {"mode": "phonon"}, muted)
                rpc.assert_not_called()


if __name__ == "__main__":
    unittest.main()
