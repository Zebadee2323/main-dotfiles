import json
from unittest.mock import patch

import agent_comms as app
from test_agent_comms import Fixture
import test_spatial_prefix as spatial_tests


class PrefixRuleTests(Fixture):
    def source(self, **options):
        return spatial_tests.SpatialContextTests().inspect(**options)

    def config(self, **rules):
        return {**app.load_config(self.paths.config), "prefix": rules}

    def test_classification_checks_visibility_before_remembered_focus(self):
        for options, expected in (
            ({"focused": "own"}, "focused"),
            ({}, "same_workspace"),
            ({"current": 2, "focused": "own"}, "different_workspace"),
            ({"attached": False, "focused": "own"}, "different_session"),
        ):
            with self.subTest(options=options):
                self.assertEqual(self.source(**options)["situation"], expected)

    def test_default_rules_preserve_existing_prefixes(self):
        for options, expected in (
            ({"focused": "own"}, ""),
            ({}, "Alice here, to your top-right."),
            ({"other_state": "none"}, ""),
            ({"current": 2}, "Alice here, in the work session, workspace 1, in the top-right."),
            ({"attached": False, "current": 2}, "Alice here, in the work session, workspace 1, in the top-right."),
        ):
            with self.subTest(options=options):
                self.assertEqual(app.expected_prefix(self.config(), "a", self.source(**options)), expected)

    def test_each_situation_can_select_independent_fields(self):
        config = self.config(
            focused={"fields": ["name"]},
            same_workspace={"fields": ["name"], "only_when_multiple_agents": False},
            different_workspace={"fields": ["workspace"], "omit_single_workspace": False},
            different_session={"fields": ["session"]},
        )
        for options, expected in (
            ({"focused": "own"}, "Alice here."),
            ({"other_state": "none"}, "Alice here."),
            ({"current": 2}, "Workspace 1."),
            ({"attached": False}, "Of the work session."),
        ):
            with self.subTest(options=options):
                source = self.source(**options)
                with patch.object(app, "checked_context", return_value=source), \
                     patch.object(app, "pane_binding", return_value="a"):
                    self.assertEqual(app.identify(self.paths, config)["prefix"], expected)
                app.validate_message(config, "a", expected + " Ready.", source=source)
                with self.assertRaises(app.ContextMismatch):
                    app.validate_message(config, "a", expected + " " + expected + " Ready.", source=source)

    def test_empty_fields_disable_each_situation(self):
        config = self.config(**{situation: {"fields": []} for situation in app.PREFIX_RULES})
        for options in ({"focused": "own"}, {}, {"current": 2}, {"attached": False}):
            with self.subTest(options=options):
                self.assertEqual(app.expected_prefix(config, "a", self.source(**options)), "")

    def test_geometry_can_be_requested_for_previously_suppressed_prefixes(self):
        config = self.config(
            focused={"fields": ["name", "position"]},
            same_workspace={"fields": ["name", "position"], "only_when_multiple_agents": False},
        )
        self.assertEqual(app.expected_prefix(config, "a", self.source(focused="own")),
                         "Alice here, in the top-right.")
        self.assertEqual(app.expected_prefix(config, "a", self.source(other_state="none")),
                         "Alice here, to your top-right.")
        config["prefix"]["focused"]["fallback_fields"] = ["name"]
        self.assertEqual(app.expected_prefix(config, "a", self.source(focused="own", geometry=False)), "Alice here.")
        config["prefix"]["focused"]["fallback_fields"] = []
        self.assertEqual(app.expected_prefix(config, "a", self.source(focused="own", geometry=False)), "")

    def test_workspace_label_and_single_workspace_policy(self):
        source = self.source(attached=False, workspace_names={"1": "main"})
        config = self.config(different_session={"fields": ["name", "workspace"]})
        self.assertEqual(app.expected_prefix(config, "a", source), "Alice here.")
        config["prefix"]["different_session"]["omit_single_workspace"] = False
        self.assertEqual(app.expected_prefix(config, "a", source), "Alice here, the main workspace.")

    def test_legacy_spatial_switch_and_explicit_rule_precedence(self):
        config = self.config(spatial=False, same_workspace={"fields": ["name", "position"]})
        self.assertEqual(app.expected_prefix(config, "a", self.source()), "Alice here, to your top-right.")
        self.assertEqual(app.expected_prefix(config, "a", self.source(current=2)), "Alice here, of the work session.")

    def test_config_validation_rejects_mistyped_rules(self):
        original = self.paths.config.read_text()
        for rule in (
            '[prefix.typo]\nfields = ["name"]',
            '[prefix.focused]\ntypo = true',
            '[prefix.focused]\nfields = "name"',
            '[prefix.focused]\nfields = ["name", "name"]',
            '[prefix.focused]\nfields = ["unknown"]',
            '[prefix.focused]\nfallback_fields = [false]',
            '[prefix.focused]\nonly_when_multiple_agents = "false"',
            '[prefix.focused]\nomit_single_workspace = 1',
        ):
            with self.subTest(rule=rule):
                self.paths.config.write_text(original + "\n" + rule)
                with self.assertRaises(app.CommsError):
                    app.load_config(self.paths.config)

    def test_listener_uses_same_rule_and_rejects_prefix_after_situation_change(self):
        self.paths.config.write_text(self.paths.config.read_text() + '''
[prefix.focused]
fields = ["name"]
[prefix.same_workspace]
fields = []
''')
        self.start_server()
        focused = self.source(focused="own")
        text = "Alice here. Ready."
        _, stream = self.request("send", id="a", msg=text, tts=text, source=focused)
        self.assertTrue(json.loads(stream.readline())["ok"])
        _, stream = self.request("send", id="a", msg=text, tts=text, source=self.source())
        self.assertFalse(json.loads(stream.readline())["ok"])

    def test_stale_prefix_without_name_is_rejected_after_prefix_disabled(self):
        app.check_prefix("a", "", "Ready.", "Ready.")
        for old in ("Alice here.", "Of the work session.", "Workspace 1.", "On the right."):
            with self.subTest(old=old), self.assertRaises(app.ContextMismatch):
                app.check_prefix("a", "", old + " Ready.", old + " Ready.")
