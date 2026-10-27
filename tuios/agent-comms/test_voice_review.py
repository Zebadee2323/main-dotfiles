"""Voice auditions never use live credentials or play real audio in tests."""
import asyncio
import io
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, Mock, patch

import agent_comms as app


class VoiceReviewTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="voice-review-test-")
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.paths = app.Paths(root / "config.toml", root / "state", root / "run")
        self.source = (
            '# Keep these comments.\n[speech]\nspeed = 0.9\n'
            '[identities.a]\nname = "Alice"\neleven_labs_voice_id = "a-voice"\n'
            'personality = """\n[identities.fake]\nvoice_approved = false\n"""\n'
            '[identities.b]\nname = "Bob"\neleven_labs_voice_id = "b-voice"\n'
            'voice_approved = true # already liked\n'
            '[identities.c]\nname = "Cleo"\neleven_labs_voice_id = "c-voice"\n'
            'voice_approved = false # retry later\n'
        )
        self.paths.config.write_text(self.source, encoding="utf-8")

    def review(self, answers, *, all_voices=False):
        audio = Mock()
        with patch.object(app.sys.stdin, "isatty", return_value=True), \
                patch.dict(app.os.environ, {"ELEVENLABS_API_KEY": "test-only"}), \
                patch.object(app.speech, "player", return_value="afplay"), \
                patch.object(app.speech, "synthesize", return_value=audio) as synthesize, \
                patch.object(app.speech, "play", new_callable=AsyncMock) as play, \
                patch("builtins.input", side_effect=answers), \
                patch("sys.stdout", new_callable=io.StringIO):
            app.voice_review(self.paths, app.load_config(self.paths.config), all_voices=all_voices)
        return synthesize, play, audio

    def test_reviews_missing_and_false_only_and_persists_each_answer(self):
        synthesize, play, audio = self.review(["invalid", "yes", "no"])
        self.assertEqual([call.args[1] for call in synthesize.call_args_list], ["a-voice", "c-voice"])
        self.assertEqual(play.await_count, 2)
        self.assertEqual(audio.remove.call_count, 2)
        config = app.load_config(self.paths.config)
        self.assertTrue(config["identities"]["a"]["voice_approved"])
        self.assertTrue(config["identities"]["b"]["voice_approved"])
        self.assertFalse(config["identities"]["c"]["voice_approved"])
        self.assertEqual(config["rejected_elevenlabs_voices"]["ids"], ["c-voice"])
        updated = self.paths.config.read_text()
        self.assertIn('# Keep these comments.', updated)
        self.assertIn('personality = """\n[identities.fake]\nvoice_approved = false\n"""', updated)
        synthesize, _, _ = self.review(["yes"])
        self.assertEqual([call.args[1] for call in synthesize.call_args_list], ["c-voice"])
        self.assertIn("voice_approved = true # retry later", self.paths.config.read_text())

    def test_quitting_preserves_prior_decisions(self):
        self.review(["yes", "q"])
        self.assertTrue(app.load_config(self.paths.config)["identities"]["a"]["voice_approved"])
        self.assertFalse(app.load_config(self.paths.config)["identities"]["c"]["voice_approved"])

    def test_identity_speed_overrides_global_speed_for_auditions(self):
        self.paths.config.write_text(self.source.replace('name = "Alice"', 'name = "Alice"\nspeed = 1.2'))
        synthesize, _, _ = self.review(["yes", "yes"])
        self.assertEqual([call.args[5] for call in synthesize.call_args_list], [1.2, 0.9])

    def test_elevenlabs_specific_speed_is_used_for_auditions(self):
        source = self.source.replace('[speech]\n', '[speech]\nelevenlabs_speed = 0.8\nphonon_speed = 1.1\n')
        source = source.replace('name = "Alice"', 'name = "Alice"\nelevenlabs_speed = 1.2\nphonon_speed = 0.7')
        self.paths.config.write_text(source)
        synthesize, _, _ = self.review(["yes", "yes"])
        self.assertEqual([call.args[5] for call in synthesize.call_args_list], [1.2, 0.8])

    def test_no_explicit_answer_leaves_config_unchanged(self):
        self.review([EOFError])
        self.assertEqual(self.paths.config.read_text(), self.source)

    def test_force_all_includes_approved_and_skip_preserves_every_approval_state(self):
        synthesize, play, audio = self.review(["skip", "s", "s"], all_voices=True)
        self.assertEqual([call.args[1] for call in synthesize.call_args_list], ["a-voice", "b-voice", "c-voice"])
        self.assertEqual(play.await_count, 3)
        self.assertEqual(audio.remove.call_count, 3)
        self.assertEqual(self.paths.config.read_text(), self.source)

    def test_force_all_can_unapprove_previously_approved_voice(self):
        self.review(["s", "no", "q"], all_voices=True)
        config = app.load_config(self.paths.config)
        self.assertNotIn("voice_approved", config["identities"]["a"])
        self.assertFalse(config["identities"]["b"]["voice_approved"])
        self.assertEqual(config["rejected_elevenlabs_voices"]["ids"], ["b-voice"])

    def test_replay_reuses_sample_without_changing_approval_or_regenerating(self):
        synthesize, play, audio = self.review(["r", "replay", "skip", "q"])
        self.assertEqual(synthesize.call_count, 2) # Alice once, then Cleo once.
        self.assertEqual(play.await_count, 4) # Alice three times, Cleo once.
        self.assertIs(play.call_args_list[0].args[0], play.call_args_list[2].args[0])
        self.assertEqual(audio.remove.call_count, 2)
        self.assertEqual(self.paths.config.read_text(), self.source)

    def test_replay_stops_previous_player_before_starting_next(self):
        events = []

        async def play(audio, executable):
            events.append("start")
            try:
                await asyncio.Event().wait()
            finally:
                events.append("stop")

        with patch.object(app.speech, "play", side_effect=play), \
                patch("builtins.input", side_effect=["r", "q"]):
            result = asyncio.run(app.voice_review_decision(Mock(), "fake", "Alice"))
        self.assertIsNone(result)
        self.assertEqual(events, ["start", "stop", "start", "stop"])

    def test_replay_after_sample_has_finished(self):
        with patch.object(app.speech, "play", AsyncMock()) as play, \
                patch("builtins.input", side_effect=["r", "yes"]):
            self.assertTrue(asyncio.run(app.voice_review_decision(Mock(), "fake", "Alice")))
        self.assertEqual(play.await_count, 2)

    def test_phonon_approvals_are_separate_and_do_not_need_cloud_credentials(self):
        source = self.source.replace('[speech]\n', '[speech]\nmode="phonon"\nphonon_speed=0.8\nelevenlabs_speed=1.1\n')
        source = source.replace('name = "Alice"', 'name = "Alice"\nphonon_voice = "Juniper"\nphonon_speed = 1.2\nelevenlabs_speed = 0.7')
        source = source.replace('name = "Bob"', 'name = "Bob"\nphonon_voice = "Easton"')
        source = source.replace('name = "Cleo"', 'name = "Cleo"\nphonon_voice = "Zoey"')
        self.paths.config.write_text(source)
        audio = Mock()
        with patch.object(app.sys.stdin, "isatty", return_value=True), \
                patch.dict(app.os.environ, {"ELEVENLABS_API_KEY": ""}), \
                patch.object(app.speech, "player", return_value="fake"), \
                patch.object(app.speech, "synthesize") as cloud, \
                patch.object(app.speech, "synthesize_phonon", AsyncMock(return_value=audio)) as local, \
                patch.object(app.speech, "play", AsyncMock()), \
                patch("builtins.input", side_effect=["yes", "no", "skip"]), \
                patch("sys.stdout", new_callable=io.StringIO):
            app.voice_review(self.paths, app.load_config(self.paths.config))
        cloud.assert_not_called()
        self.assertEqual([call.args[1] for call in local.call_args_list], ["Juniper", "Easton", "Zoey"])
        self.assertEqual([call.args[4] for call in local.call_args_list], [1.2, 0.8, 0.8])
        self.assertEqual(audio.remove.call_count, 3)
        config = app.load_config(self.paths.config)
        self.assertTrue(config["identities"]["a"]["phonon_voice_approved"])
        self.assertFalse(config["identities"]["b"]["phonon_voice_approved"])
        self.assertNotIn("phonon_voice_approved", config["identities"]["c"])
        self.assertTrue(config["identities"]["b"]["voice_approved"])
        self.assertNotIn("voice_approved", config["identities"]["a"])
        self.assertNotIn("rejected_elevenlabs_voices", config)
        self.assertIn('voice_approved = true # already liked', self.paths.config.read_text())

    def test_phonon_changed_voice_is_not_approved(self):
        self.paths.config.write_text(self.source.replace('name = "Alice"', 'name = "Alice"\nphonon_voice = "Juniper"'))
        original = self.paths.config.read_text()
        with self.assertRaisesRegex(app.CommsError, "changed during review"):
            app.save_voice_approval(self.paths, "a", "Zoey", True, mode="phonon")
        self.assertEqual(self.paths.config.read_text(), original)

    def test_phonon_default_skips_only_phonon_approved_voices(self):
        config = {"speech": {"mode": "phonon"}, "identities": {
            "a": {"name": "Alice", "voice_approved": False, "phonon_voice_approved": True}}}
        with patch("sys.stdout", new_callable=io.StringIO) as output:
            app.voice_review(self.paths, config)
        self.assertIn("All identity voices are approved", output.getvalue())

    def test_rejects_non_boolean_phonon_approval(self):
        self.paths.config.write_text(self.source + '\nphonon_voice_approved = "yes"\n')
        with self.assertRaisesRegex(app.CommsError, "phonon_voice_approved must be a boolean"):
            app.load_config(self.paths.config)

    def test_changed_voice_is_not_approved(self):
        with self.assertRaisesRegex(app.CommsError, "changed during review"):
            app.save_voice_approval(self.paths, "a", "old-voice", True)
        self.assertEqual(self.paths.config.read_text(), self.source)

    def test_rejections_are_unique_and_preserved_after_later_approval(self):
        app.save_voice_approval(self.paths, "c", "c-voice", False)
        app.save_voice_approval(self.paths, "c", "c-voice", False)
        app.save_voice_approval(self.paths, "a", "a-voice", False)
        app.save_voice_approval(self.paths, "c", "c-voice", True)
        config = app.load_config(self.paths.config)
        self.assertEqual(config["rejected_elevenlabs_voices"]["ids"], ["a-voice", "c-voice"])
        self.assertTrue(config["identities"]["c"]["voice_approved"])

    def test_rejection_preserves_multiline_list_comments_and_personality(self):
        source = self.source + '\n[rejected_elevenlabs_voices]\n# Keep history\nids = [\n "older", # Keep this note\n]\n'
        self.paths.config.write_text(source)
        app.save_voice_approval(self.paths, "c", "c-voice", False)
        self.assertEqual(app.load_config(self.paths.config)["rejected_elevenlabs_voices"]["ids"], ["c-voice", "older"])
        self.assertIn('# Keep this note', self.paths.config.read_text())

    def test_rejection_table_without_ids_is_supported(self):
        self.paths.config.write_text(self.source + '\n[rejected_elevenlabs_voices]\n# Reserved\n')
        app.save_voice_approval(self.paths, "c", "c-voice", False)
        self.assertEqual(app.load_config(self.paths.config)["rejected_elevenlabs_voices"]["ids"], ["c-voice"])

    def test_rejects_invalid_rejection_list(self):
        for value in ('"voice"', '[false]', '[""]'):
            self.paths.config.write_text(self.source + f'\n[rejected_elevenlabs_voices]\nids = {value}\n')
            with self.assertRaisesRegex(app.CommsError, 'rejected_elevenlabs_voices.ids'):
                app.load_config(self.paths.config)

    def test_rejects_non_boolean_approval(self):
        self.paths.config.write_text(self.source.replace("voice_approved = true #", 'voice_approved = "yes" #'))
        with self.assertRaisesRegex(app.CommsError, "must be a boolean"):
            app.load_config(self.paths.config)

    def test_all_approved_needs_no_audio_or_terminal(self):
        with patch("sys.stdout", new_callable=io.StringIO) as output:
            app.voice_review(self.paths, {"identities": {"a": {"voice_approved": True}}})
        self.assertIn("All identity voices are approved", output.getvalue())

    def test_parser_accepts_config_after_command(self):
        args = app.parser().parse_args(["voice-review", "--all", "--config", str(self.paths.config)])
        self.assertEqual(args.command, "voice-review")
        self.assertEqual(args.config, str(self.paths.config))
        self.assertTrue(args.all)

    def test_decision_during_playback_stops_audio_before_returning(self):
        for answer, expected in (("yes", True), ("no", False), ("s", "skip"), ("skip", "skip"), ("q", None), (EOFError, None)):
            with self.subTest(answer=answer):
                started = threading.Event()
                stopped = threading.Event()

                async def play(audio, executable):
                    started.set()
                    try:
                        await asyncio.Event().wait()
                    finally:
                        stopped.set()

                def decide(prompt):
                    self.assertTrue(started.wait(timeout=2))
                    self.assertFalse(stopped.is_set())
                    if answer is EOFError:
                        raise EOFError
                    return answer

                with patch.object(app.speech, "play", side_effect=play), \
                        patch("builtins.input", side_effect=decide):
                    result = asyncio.run(app.voice_review_decision(Mock(), "afplay", "Alice"))
                self.assertEqual(result, expected)
                self.assertTrue(stopped.is_set())


if __name__ == "__main__":
    unittest.main()
