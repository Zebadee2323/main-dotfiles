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

    def test_provider_speed_validation_at_both_scopes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            for key in ("elevenlabs_speed", "phonon_speed"):
                for value in ("0.7", "1", "1.2", "0.69", "1.21", "nan", "inf", "true", '"1.0"', "[]"):
                    for scope in ("global", "identity"):
                        with self.subTest(key=key, value=value, scope=scope):
                            setting = f"{key} = {value}\n"
                            path.write_text(('[speech]\n' + setting if scope == "global" else '')
                                            + '[identities.a]\nname="Alice"\n'
                                            + (setting if scope == "identity" else ''))
                            if value in ("0.7", "1", "1.2"):
                                app.load_config(path)
                            else:
                                with self.assertRaisesRegex(app.CommsError, key):
                                    app.load_config(path)

    def test_provider_precedence_and_independence(self):
        for mode, other in (("phonon", "elevenlabs"), ("elevenlabs", "phonon")):
            key = f"{mode}_speed"
            cases = (({}, {}, 1.0), ({"speed": 0.8}, {}, 0.8),
                     ({"speed": 0.8, key: 0.9}, {}, 0.9),
                     ({key: 0.9}, {"speed": 1.1}, 1.1),
                     ({key: 0.9}, {"speed": 1.1, key: 1.2}, 1.2),
                     ({f"{other}_speed": 1.2}, {f"{other}_speed": 0.7}, 1.0))
            for settings, metadata, expected in cases:
                with self.subTest(mode=mode, settings=settings, metadata=metadata):
                    self.assertEqual(app.speech_speed(settings, metadata, mode), expected)

    def test_provider_overrides_propagate_without_inventing_defaults(self):
        config = {"identities": {"a": {"name": "Alice", "phonon_speed": 1.1, "elevenlabs_speed": 0.9}}}
        message = app.validate_message(config, "a", "Hello")
        self.assertEqual(message["phonon_speed"], 1.1)
        self.assertEqual(message["elevenlabs_speed"], 0.9)
        self.assertNotIn("voice_speed", message)
        config["identities"]["a"] = {"name": "Alice"}
        message = app.validate_message(config, "a", "Hello")
        self.assertNotIn("phonon_speed", message)
        self.assertNotIn("elevenlabs_speed", message)


class WatcherVoiceSpeedTests(unittest.IsolatedAsyncioTestCase):
    async def test_watcher_routes_only_selected_provider_speed(self):
        for mode, expected in (("phonon", 1.2), ("elevenlabs", 0.8)):
            with self.subTest(mode=mode):
                message = {"uid": "m", "phase": "preparing", "voice_id": "v", "phonon_voice": "Juniper",
                           "tts": "Hello", "phonon_speed": 1.2, "elevenlabs_speed": 0.8, "voice_speed": 1.0}

                async def rpc(paths, command, **kwargs):
                    if command == "claim-speech":
                        return {"claimed": True, "lease": "test"}
                    message.update(phase="displaying", started_at=0)

                with patch.dict(app.os.environ, {"ELEVENLABS_API_KEY": "fake"}), \
                        patch.object(app, "rpc", side_effect=rpc), patch.object(app, "AudioWorkspace"), \
                        patch.object(app.speech, "player", return_value="fake"), \
                        patch.object(app.speech, "synthesize", return_value=Mock(duration=1)) as cloud, \
                        patch.object(app.speech, "synthesize_phonon", AsyncMock(return_value=Mock(duration=1))) as local, \
                        patch.object(app.speech, "play", AsyncMock()):
                    await app.watcher_speech(None, {"message": message}, {"mode": mode, "normalize": False}, False)
                    selected, unused = (local, cloud) if mode == "phonon" else (cloud, local)
                    self.assertEqual(selected.call_args.args[-1], expected)
                    unused.assert_not_called()

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
