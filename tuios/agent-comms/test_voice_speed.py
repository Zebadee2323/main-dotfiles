"""Voice-profile speed validation, snapshot propagation, and watcher playback."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

import agent_comms as app
import pane_identity
from test_agent_comms import Fixture, write_config

MALE = '[voice_groups.main.voices.m]\ngender = "male"\n'


def profile_config(path, profile="", body=""):
    """A config whose female profile "a" carries the given extra settings."""
    write_config(path, body, voices=f'[voice_groups.main.voices.a]\ngender = "female"\n{profile}' + MALE)


class VoiceSpeedTests(unittest.TestCase):
    def test_config_validates_profile_speed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            for value in ("0.7", "1", "1.2", "0.69", "1.21", "nan", "inf", "-inf", "true", '"1.0"', "[]", "{}"):
                with self.subTest(value=value):
                    profile_config(path, f"speed = {value}\n")
                    if value in ("0.7", "1", "1.2"):
                        self.assertEqual(app.load_config(path)["voices"]["a"]["speed"], float(value))
                    else:
                        with self.assertRaisesRegex(app.CommsError, "Voice profile a: speed"):
                            app.load_config(path)

    def test_message_carries_only_explicit_profile_override(self):
        config = {"speech": {"speed": 0.9}}
        profile = {"gender": "female"}
        self.assertNotIn("voice_speed", app.validate_message(config, "Alice", "Hello", profile=profile))
        profile["speed"] = 1.2
        self.assertEqual(app.validate_message(config, "Alice", "Hello", profile=profile)["voice_speed"], 1.2)

    def test_provider_speed_validation_at_both_scopes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            for key in ("elevenlabs_speed", "phonon_speed"):
                for value in ("0.7", "1", "1.2", "0.69", "1.21", "nan", "inf", "true", '"1.0"', "[]"):
                    for scope in ("global", "profile"):
                        with self.subTest(key=key, value=value, scope=scope):
                            setting = f"{key} = {value}\n"
                            profile_config(path, setting if scope == "profile" else "",
                                           "[speech]\n" + setting if scope == "global" else "")
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
        profile = {"gender": "female", "phonon_speed": 1.1, "elevenlabs_speed": 0.9}
        message = app.validate_message({}, "Alice", "Hello", profile=profile)
        self.assertEqual(message["phonon_speed"], 1.1)
        self.assertEqual(message["elevenlabs_speed"], 0.9)
        self.assertNotIn("voice_speed", message)
        message = app.validate_message({}, "Alice", "Hello", profile={"gender": "female"})
        self.assertNotIn("phonon_speed", message)
        self.assertNotIn("elevenlabs_speed", message)


class VoiceSnapshotTests(Fixture):
    """Accepted messages use the binding's voice snapshot, never the live config."""

    ORIGINAL = ('[voice_groups.main.voices.voice_f]\ngender = "female"\neleven_labs_voice_id = "before"\n'
                'phonon_voice = "Juniper"\npocket_tts_voice = "voices/alice.wav"\nspeed = 0.8\n'
                'phonon_speed = 1.1\nelevenlabs_speed = 0.9\n'
                '[voice_groups.main.voices.voice_f2]\ngender = "female"\neleven_labs_voice_id = "other"\n'
                '[voice_groups.main.voices.voice_m]\ngender = "male"\neleven_labs_voice_id = ""\n')
    EXPECTED = {"voice_id": "before", "phonon_voice": "Juniper", "voice_speed": 0.8,
                "phonon_speed": 1.1, "elevenlabs_speed": 0.9}

    def setUp(self):
        super().setUp()
        write_config(self.paths.config, 'display_seconds = 0.18\n', voices=self.ORIGINAL)
        self.alice = self.sender("Alice")
        self.pocket = str(self.paths.config.parent.resolve() / "voices/alice.wav")
        epoch, binding = pane_identity.lookup(self.paths, self.alice["pane_env"])
        self.assertEqual(binding["voice_profile_id"], "voice_f")
        self.assertEqual(binding["voice_profile"]["pocket_tts_voice"], self.pocket)
        self.assertTrue(Path(binding["voice_profile"]["pocket_tts_voice"]).is_absolute())
        self.snapshot = binding["voice_profile"]

    def assert_snapshot_delivery(self):
        _, binding = pane_identity.lookup(self.paths, self.alice["pane_env"])
        self.assertEqual(binding["voice_profile"], self.snapshot)
        self.assertEqual(binding["voice_profile_id"], "voice_f")
        self.start_server()
        stream = self.subscribe()
        _, sender = self.request("send", **self.alice, msg="hello")
        self.assertTrue(json.loads(sender.readline())["ok"])
        message = self.message(stream)
        self.assertEqual({key: message.get(key) for key in self.EXPECTED}, self.EXPECTED)
        self.assertEqual(message["pocket_tts_voice"], self.pocket)
        self.assertEqual(message["voice_profile_id"], "voice_f")
        record = json.loads(self.cli("history", "--json").stdout)[-1]
        self.assertEqual(record["voice_id"], "before")
        self.assertEqual(record["pocket_tts_voice"], self.pocket)

    def test_editing_source_profile_after_allocation_keeps_accepted_voice(self):
        write_config(self.paths.config, 'display_seconds = 0.18\n', voices=self.ORIGINAL.replace(
            '"before"', '"after"').replace('"Juniper"', '"Other"').replace("voices/alice.wav", "voices/changed.wav")
            .replace("speed = 0.8", "speed = 1.2").replace("phonon_speed = 1.1", "phonon_speed = 0.7"))
        self.assertEqual(app.load_config(self.paths.config)["voices"]["voice_f"]["eleven_labs_voice_id"], "after")
        self.assert_snapshot_delivery()

    def test_deleting_source_profile_after_allocation_keeps_accepted_voice(self):
        remaining = self.ORIGINAL[self.ORIGINAL.index("[voice_groups.main.voices.voice_f2]"):]
        write_config(self.paths.config, 'display_seconds = 0.18\n', voices=remaining)
        self.assertNotIn("voice_f", app.load_config(self.paths.config)["voices"])
        self.assert_snapshot_delivery()


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

    async def test_playback_prefers_profile_then_global_then_default(self):
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
