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

    def review(self, answers):
        audio = Mock()
        with patch.object(app.sys.stdin, "isatty", return_value=True), \
                patch.dict(app.os.environ, {"ELEVENLABS_API_KEY": "test-only"}), \
                patch.object(app.speech, "player", return_value="afplay"), \
                patch.object(app.speech, "synthesize", return_value=audio) as synthesize, \
                patch.object(app.speech, "play", new_callable=AsyncMock) as play, \
                patch("builtins.input", side_effect=answers), \
                patch("sys.stdout", new_callable=io.StringIO):
            app.voice_review(self.paths, app.load_config(self.paths.config))
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
        self.assertEqual(config["rejected_voices"]["ids"], ["c-voice"])
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

    def test_no_explicit_answer_leaves_config_unchanged(self):
        self.review([EOFError])
        self.assertEqual(self.paths.config.read_text(), self.source)

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
        self.assertEqual(config["rejected_voices"]["ids"], ["a-voice", "c-voice"])
        self.assertTrue(config["identities"]["c"]["voice_approved"])

    def test_rejection_preserves_multiline_list_comments_and_personality(self):
        source = self.source + '\n[rejected_voices]\n# Keep history\nids = [\n "older", # Keep this note\n]\n'
        self.paths.config.write_text(source)
        app.save_voice_approval(self.paths, "c", "c-voice", False)
        self.assertEqual(app.load_config(self.paths.config)["rejected_voices"]["ids"], ["c-voice", "older"])
        self.assertIn('# Keep this note', self.paths.config.read_text())

    def test_rejection_table_without_ids_is_supported(self):
        self.paths.config.write_text(self.source + '\n[rejected_voices]\n# Reserved\n')
        app.save_voice_approval(self.paths, "c", "c-voice", False)
        self.assertEqual(app.load_config(self.paths.config)["rejected_voices"]["ids"], ["c-voice"])

    def test_rejects_invalid_rejection_list(self):
        for value in ('"voice"', '[false]', '[""]'):
            self.paths.config.write_text(self.source + f'\n[rejected_voices]\nids = {value}\n')
            with self.assertRaisesRegex(app.CommsError, 'rejected_voices.ids'):
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
        args = app.parser().parse_args(["voice-review", "--config", str(self.paths.config)])
        self.assertEqual(args.command, "voice-review")
        self.assertEqual(args.config, str(self.paths.config))

    def test_decision_during_playback_stops_audio_before_returning(self):
        for answer, expected in (("yes", True), ("no", False), ("q", None), (EOFError, None)):
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
                self.assertIs(result, expected)
                self.assertTrue(stopped.is_set())


if __name__ == "__main__":
    unittest.main()
