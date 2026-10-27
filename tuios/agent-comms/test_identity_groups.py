"""Grouped source config, active bag membership, and stable existing bindings."""
import json
from unittest.mock import patch

import agent_comms as app
import pane_identity
from test_agent_comms import Fixture


SOURCE = '''# Preserve grouping and comments.
active_groups = ["pocket"]
[speech]
mode = "pocket_tts"
[identity_groups.original.identities.a]
name = "Alice"
phonon_voice = "Juniper"
voice_approved = true
[identity_groups.original.identities.b]
name = "Bob"
[identity_groups.pocket.identities.c]
name = "Cleo"
pocket_tts_voice = "alba"
[identity_groups.pocket.identities.d]
name = "Dora"
pocket_tts_voice = "anna"
'''


class IdentityGroupsTests(Fixture):
    def config(self, source=SOURCE):
        self.paths.config.write_text(source)
        return app.load_config(self.paths.config)

    def test_runtime_indexes_all_but_bag_contains_only_active_groups(self):
        config = self.config()
        self.assertEqual(set(config["identities"]), {"a", "b", "c", "d"})
        self.assertEqual(app.active_identity_ids(config), {"c", "d"})
        draws = [app.draw_id(self.paths, config) for _ in range(6)]
        self.assertEqual(set(draws), {"c", "d"})
        self.assertEqual(set(draws[:2]), {"c", "d"})
        self.assertEqual(json.loads((self.paths.state / "bag.json").read_text())["ids"], ["c", "d"])

    def test_switching_active_groups_rebuilds_bag(self):
        config = self.config()
        app.draw_id(self.paths, config)
        config = self.config(SOURCE.replace('["pocket"]', '["original"]'))
        self.assertEqual({app.draw_id(self.paths, config) for _ in range(4)}, {"a", "b"})
        config = self.config(SOURCE.replace('["pocket"]', '["original", "pocket"]'))
        self.assertEqual({app.draw_id(self.paths, config) for _ in range(4)}, {"a", "b", "c", "d"})

    def test_omitted_active_groups_includes_all_and_legacy_still_works(self):
        config = self.config(SOURCE.replace('active_groups = ["pocket"]\n', '')
                             + '\n[identities.e]\nname="Legacy"\n')
        self.assertEqual(app.active_identity_ids(config), {"a", "b", "c", "d", "e"})

    def test_invalid_active_groups_fail_closed(self):
        for value in ('[]', '["missing"]', '["pocket", "pocket"]', '"pocket"', '[false]', '[[]]'):
            with self.subTest(value=value), self.assertRaisesRegex(app.CommsError, "active_groups"):
                self.config(SOURCE.replace('["pocket"]', value))

    def test_duplicate_ids_and_malformed_groups_are_rejected(self):
        for suffix in ('\n[identities.c]\nname="Duplicate"\n',
                       '\n[identity_groups.extra.identities.c]\nname="Duplicate"\n',
                       '\n[identity_groups.empty]\n',
                       '\n[identity_groups."bad group".identities.e]\nname="Bad"\n'):
            with self.subTest(suffix=suffix), self.assertRaises(app.CommsError):
                self.config(SOURCE + suffix)

    def test_approval_updates_grouped_source_without_flattening_or_touching_other_groups(self):
        self.config()
        app.save_voice_approval(self.paths, "c", "alba", True, mode="pocket_tts")
        self.assertEqual(self.paths.config.read_text(), SOURCE.replace(
            '[identity_groups.pocket.identities.c]',
            '[identity_groups.pocket.identities.c]\npocket_tts_voice_approved = true'))
        app.save_voice_approval(self.paths, "c", "alba", False, mode="pocket_tts")
        config = app.load_config(self.paths.config)
        self.assertFalse(config["identities"]["c"]["pocket_tts_voice_approved"])
        self.assertTrue(config["identities"]["a"]["voice_approved"])
        self.assertNotIn("rejected_elevenlabs_voices", config)

    def test_active_exhaustion_is_explicit_and_inactive_bindings_stay_stable(self):
        config = self.config()
        env = {"TUIOS_ENV": "1", "TUIOS_PANE_ID": "one", "TUIOS_SESSION": "test"}
        with patch.object(pane_identity, "inventory", return_value=None):
            first = app.pane_binding(self.paths, config, env=env)
            self.assertIn(first, {"c", "d"})
            app.pane_binding(self.paths, config, env={**env, "TUIOS_PANE_ID": "two"})
            with self.assertRaisesRegex(app.CommsError, "No unassigned"):
                app.pane_binding(self.paths, config, env={**env, "TUIOS_PANE_ID": "three"})
            config = self.config(SOURCE.replace('["pocket"]', '["original"]'))
            self.assertEqual(app.pane_binding(self.paths, config, env=env), first)
            self.assertEqual(app.pane_binding(self.paths, config, allocate=False, env=env), first)
            app.pane_binding(self.paths, config, clear=True, env=env)
            self.assertIn(app.pane_binding(self.paths, config, env=env), {"a", "b"})
