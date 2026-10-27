"""Identity speed validation, message propagation, and watcher playback."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

import agent_comms as app


class VoiceSpeedTests(unittest.TestCase):
    def test_config_validates_identity_speed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            for value in ("0.7", "1", "1.2", "0.69", "1.21", "nan", "inf", "-inf", "true", '"1.0"', "[]", "{}"):
                with self.subTest(value=value):
                    path.write_text(f'[identities.a]\nname = "Alice"\nspeed = {value}\n')
                    if value in ("0.7", "1", "1.2"):
                        self.assertEqual(app.load_config(path)["identities"]["a"]["speed"], float(value))
                    else:
                        with self.assertRaisesRegex(app.CommsError, "Identity a: speed"):
                            app.load_config(path)

    def test_message_carries_only_explicit_identity_override(self):
        config = {"speech": {"speed": 0.9}, "identities": {"a": {"name": "Alice"}}}
        self.assertNotIn("voice_speed", app.validate_message(config, "a", "Hello"))
        config["identities"]["a"]["speed"] = 1.2
        self.assertEqual(app.validate_message(config, "a", "Hello")["voice_speed"], 1.2)


class WatcherVoiceSpeedTests(unittest.IsolatedAsyncioTestCase):
    async def test_playback_prefers_identity_then_global_then_default(self):
        for override, global_speed, expected in ((1.2, 0.9, 1.2), (None, 0.9, 0.9), (None, None, 1.0)):
            with self.subTest(override=override, global_speed=global_speed):
                message = {"uid": "m", "phase": "preparing", "voice_id": "v", "tts": "Hello"}
                if override is not None:
                    message["voice_speed"] = override
                settings = {"normalize": False}
                if global_speed is not None:
                    settings["speed"] = global_speed
                state = {"message": message}

                async def rpc(paths, command, **kwargs):
                    if command == "claim-speech":
                        return {"claimed": True, "lease": "test"}
                    message.update(phase="displaying", started_at=0)

                with patch.dict(app.os.environ, {"ELEVENLABS_API_KEY": "fake"}), \
                        patch.object(app, "rpc", side_effect=rpc), \
                        patch.object(app, "AudioWorkspace") as workspace, \
                        patch.object(app.speech, "player", return_value="fake"), \
                        patch.object(app.speech, "synthesize", return_value=Mock(duration=1)) as synthesize, \
                        patch.object(app.speech, "play", new_callable=AsyncMock):
                    await app.watcher_speech(None, state, settings, False)
                    self.assertEqual(synthesize.call_args.args[5], expected)
                    workspace.return_value.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
