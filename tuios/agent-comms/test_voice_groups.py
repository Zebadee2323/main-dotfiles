"""Grouped voice profiles, active groups for new assignments, and stable binding snapshots."""
import json
import unittest
from unittest.mock import patch

import agent_comms as app
import allocation
import pane_identity
from test_agent_comms import Fixture, write_config


BODY = '''active_groups = ["pocket"]
[speech]
mode = "pocket_tts"
'''
VOICES = '''# Preserve grouping and comments.
[voice_groups.original.voices.orig_f]
gender = "female"
phonon_voice = "Juniper"
voice_approved = true
[voice_groups.original.voices.orig_m]
gender = "male"
[voice_groups.pocket.voices.pocket_f]
gender = "female"
pocket_tts_voice = "alba"
[voice_groups.pocket.voices.pocket_m]
gender = "male"
pocket_tts_voice = "anna"
'''
ORIGINAL = {"orig_f", "orig_m"}
POCKET = {"pocket_f", "pocket_m"}


class VoiceGroupsTests(Fixture):
    def config(self, body=BODY, voices=VOICES):
        write_config(self.paths.config, body, voices=voices)
        return app.load_config(self.paths.config)

    def bind(self, config, pane, **kwargs):
        with patch.object(pane_identity, "inventory", return_value=None):
            return app.pane_binding(self.paths, config, env=self.pane_env(pane), **kwargs)

    def test_runtime_indexes_all_profiles_but_only_active_groups_are_assigned(self):
        config = self.config()
        self.assertEqual(set(config["voices"]), ORIGINAL | POCKET)
        self.assertEqual(config["voice_group_of"], {"orig_f": "original", "orig_m": "original",
                                                    "pocket_f": "pocket", "pocket_m": "pocket"})
        self.assertEqual(config["active_voice_groups"], ["pocket"])
        self.assertEqual(allocation.eligible_profiles(config), {"female": ["pocket_f"], "male": ["pocket_m"]})
        bindings = [self.bind(config, f"pane-{i}")[1] for i in range(6)]
        for binding in bindings:
            self.assertIn(binding["voice_profile_id"], POCKET)
            self.assertEqual(binding["voice_group"], "pocket")
            self.assertEqual(binding["voice_profile"]["gender"], binding["gender"])
        state = json.loads((self.paths.state / allocation.STATE_FILE).read_text())
        self.assertEqual({gender: queue["eligible"] for gender, queue in state["voices"].items()},
                         {"female": ["pocket_f"], "male": ["pocket_m"]})

    def test_switching_active_groups_affects_only_new_assignments(self):
        config = self.config()
        first = self.bind(config, "one")
        config = self.config(BODY.replace('["pocket"]', '["original"]'))
        self.assertEqual(self.bind(config, "one"), first)
        drawn = {self.bind(config, f"orig-{i}")[1]["voice_profile_id"] for i in range(4)}
        self.assertTrue(drawn and drawn <= ORIGINAL, drawn)
        config = self.config(BODY.replace('["pocket"]', '["original", "pocket"]'))
        bindings = [self.bind(config, f"both-{i}")[1] for i in range(8)]
        self.assertLessEqual({b["voice_profile_id"] for b in bindings}, ORIGINAL | POCKET)
        for gender, profiles in (("female", {"orig_f", "pocket_f"}), ("male", {"orig_m", "pocket_m"})):
            drawn = [b["voice_profile_id"] for b in bindings if b["gender"] == gender]
            self.assertLessEqual(set(drawn), profiles)
        self.assertEqual(self.bind(config, "one"), first)

    def test_omitted_active_groups_includes_all(self):
        config = self.config(BODY.replace('active_groups = ["pocket"]\n', ''))
        self.assertEqual(config["active_voice_groups"], ["original", "pocket"])
        self.assertEqual(allocation.eligible_profiles(config),
                         {"female": ["orig_f", "pocket_f"], "male": ["orig_m", "pocket_m"]})

    def test_invalid_active_groups_fail_closed(self):
        for value in ('[]', '["missing"]', '["pocket", "pocket"]', '"pocket"', '[false]'):
            with self.subTest(value=value), self.assertRaisesRegex(app.CommsError, "active_groups"):
                self.config(BODY.replace('["pocket"]', value))

    def test_nested_active_groups_fail_closed(self):
        with self.assertRaisesRegex(app.CommsError, "active_groups"):
            self.config(BODY.replace('["pocket"]', '[[]]'))

    def test_active_groups_must_cover_every_name_gender(self):
        voices = VOICES.replace('[voice_groups.pocket.voices.pocket_m]\ngender = "male"\n'
                                'pocket_tts_voice = "anna"\n', '')
        with self.assertRaisesRegex(app.CommsError, "no active male voice profile"):
            self.config(voices=voices)

    def test_duplicate_profile_ids_and_malformed_groups_are_rejected(self):
        for suffix, error in (('[voice_groups.extra.voices.pocket_f]\ngender = "female"\n', "Duplicate voice profile"),
                              ('[voice_groups.empty]\n', "nonempty voices table"),
                              ('[voice_groups."bad group".voices.e]\ngender = "male"\n', "Invalid voice group"),
                              ('[voice_groups.extra.voices."bad id"]\ngender = "male"\n', "Invalid voice profile"),
                              ('[voice_groups.extra.voices.e]\neleven_labs_voice_id = ""\n', "needs gender"),
                              ('[voice_groups.extra.voices.e]\ngender = "other"\n', "needs gender"),
                              ('[voice_groups.extra.voices.e]\ngender = "male"\nname = "Eve"\n', "unsupported field")):
            with self.subTest(suffix=suffix), self.assertRaisesRegex(app.CommsError, error):
                self.config(voices=VOICES + suffix)

    def test_legacy_identity_tables_are_rejected_with_conversion_guidance(self):
        for legacy in ('[identities.a]\nname = "Alice"\n',
                       '[identity_groups.original.identities.a]\nname = "Alice"\n'):
            with self.subTest(legacy=legacy), \
                    self.assertRaisesRegex(app.CommsError, r"Legacy \[identities\]/\[identity_groups\].*"
                                                           r"voice_groups\.GROUP\.voices\.PROFILE_ID"):
                self.config(voices=VOICES + legacy)

    def test_approval_updates_grouped_source_without_flattening_or_touching_other_groups(self):
        self.config()
        source = self.paths.config.read_text()
        app.save_voice_approval(self.paths, "pocket_f", "alba", True, mode="pocket_tts")
        self.assertEqual(self.paths.config.read_text(), source.replace(
            '[voice_groups.pocket.voices.pocket_f]',
            '[voice_groups.pocket.voices.pocket_f]\npocket_tts_voice_approved = true'))
        app.save_voice_approval(self.paths, "pocket_f", "alba", False, mode="pocket_tts")
        config = app.load_config(self.paths.config)
        self.assertFalse(config["voices"]["pocket_f"]["pocket_tts_voice_approved"])
        self.assertTrue(config["voices"]["orig_f"]["voice_approved"])
        self.assertNotIn("rejected_elevenlabs_voices", config)

    def test_existing_binding_keeps_snapshot_after_deactivation_edit_or_deletion(self):
        config = self.config()
        epoch, first = self.bind(config, "one")
        profile = first["voice_profile_id"]
        self.assertEqual(first["voice_profile"], config["voices"][profile])
        # Deactivate the group and edit the bound profile's voice.
        edited = self.config(BODY.replace('["pocket"]', '["original"]'),
                             VOICES.replace('"alba"', '"edited"').replace('"anna"', '"edited"'))
        self.assertEqual(self.bind(edited, "one"), (epoch, first))
        # Delete the group entirely; the binding and its snapshot survive.
        deleted = self.config(BODY.replace('["pocket"]', '["original"]'),
                              VOICES[:VOICES.index("[voice_groups.pocket")])
        self.assertNotIn(profile, deleted["voices"])
        self.assertEqual(self.bind(deleted, "one", allocate=False), (epoch, first))
        self.assertEqual(self.bind(deleted, "one"), (epoch, first))
        # The listener builds delivery from the snapshot, never live config.
        server = app.Server(self.paths, deleted, quiet=True)
        message, _ = server.accept({"name": first["name"], "epoch": epoch, "msg": "Ready."}, self.pane_env("one"))
        self.assertEqual((message["voice_profile_id"], message["pocket_tts_voice"]),
                         (profile, first["voice_profile"]["pocket_tts_voice"]))
        # After clearing, the next assignment uses only the now-active group.
        app.pane_binding(self.paths, deleted, clear=True, env=self.pane_env("one"))
        self.assertIn(self.bind(deleted, "one")[1]["voice_profile_id"], ORIGINAL)
