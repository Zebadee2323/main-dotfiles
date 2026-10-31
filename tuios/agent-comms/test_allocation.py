"""Name/voice bag allocation, durable state, reset, and migration. All state is isolated."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import agent_comms as app
import allocation
import pane_identity
from test_agent_comms import ROOT, TEST_PANE, Fixture, write_config


def names_toml(female=(), male=()):
    return (f"female = {json.dumps(list(female))}\nmale = {json.dumps(list(male))}\n")


def voice(profile, gender, group="main", extra=""):
    return f'[voice_groups.{group}.voices.{profile}]\ngender = "{gender}"\neleven_labs_voice_id = "{profile}-id"\n{extra}'


class CatalogTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="agent-comms-catalog-")
        self.addCleanup(temp.cleanup)
        self.config = Path(temp.name) / "config.toml"

    def load(self, names, voices=voice("v_f", "female") + voice("v_m", "male"), body=""):
        write_config(self.config, body, names=names, voices=voices)
        return app.load_config(self.config)

    def test_valid_catalog_and_shipped_catalog_has_hundreds_of_names(self):
        config = self.load(names_toml(["Alice", "Maya"], ["Ben"]))
        self.assertEqual(sorted(config["names"]), [("Alice", "female"), ("Ben", "male"), ("Maya", "female")])
        shipped = allocation.load_names(ROOT / "names.toml")
        self.assertGreaterEqual(len(shipped), 300)
        self.assertEqual({gender for _, gender in shipped}, {"male", "female"})

    def test_invalid_duplicate_reserved_and_empty_names_are_rejected(self):
        for names, error in ((names_toml(["Alice2"]), "Invalid female name"),
                             (names_toml(["alice"]), "Invalid female name"),
                             (names_toml(["Mary Ann"]), "Invalid female name"),
                             (names_toml(["Zoë"]), "Invalid female name"),
                             (names_toml(["Alice"], ["ALICE"]), "Duplicate name"),
                             (names_toml(["Alice", "Alice"]), "Duplicate name"),
                             (names_toml(["User"]), "reserved"),
                             (names_toml(), "at least one name"),
                             ('female = ["Alice"]\nother = ["Sam"]\n', "Unknown name catalog key"),
                             ('female = "Alice"\n', "must be an array")):
            with self.subTest(names=names), self.assertRaisesRegex(app.CommsError, error):
                self.load(names)

    def test_every_represented_gender_needs_an_active_voice(self):
        with self.assertRaisesRegex(app.CommsError, "no active male voice"):
            self.load(names_toml(["Alice"], ["Ben"]), voices=voice("v_f", "female"))
        with self.assertRaisesRegex(app.CommsError, "no active male voice"):
            self.load(names_toml(["Alice"], ["Ben"]), body='active_groups = ["women"]\n',
                      voices=voice("v_f", "female", "women") + voice("v_m", "male", "men"))
        self.load(names_toml(["Alice"]), voices=voice("v_f", "female"))  # Unrepresented genders need no voice.

    def test_profiles_need_explicit_gender_and_no_names_or_legacy_tables(self):
        for voices, error in ((voice("v", "unknown"), "needs gender"),
                              ('[voice_groups.main.voices.v]\neleven_labs_voice_id = "x"\n', "needs gender"),
                              (voice("v", "female", extra='name = "Alice"\n'), "unsupported field"),
                              (voice("v", "female") + voice("v", "male", "other"), "Duplicate voice profile"),
                              (voice("v", "female", extra="speed = 2.0\n"), "between 0.7 and 1.2"),
                              (voice("v", "female", extra="voice_approved = 1\n"), "boolean"),
                              ('[identities.a]\nname = "Alice"\n', "Legacy")):
            with self.subTest(voices=voices), self.assertRaisesRegex(app.CommsError, error):
                self.load(names_toml(["Alice"]), voices=voices)

    def test_names_file_is_required_and_relative_to_config(self):
        self.config.parent.mkdir(parents=True, exist_ok=True)
        self.config.write_text(voice("v_f", "female"), encoding="utf-8")
        with self.assertRaisesRegex(app.CommsError, "names_file"):
            app.load_config(self.config)
        other = Path(tempfile.mkdtemp(dir=self.config.parent))
        (other / "custom.toml").write_text(names_toml(["Alice"]), encoding="utf-8")
        self.config.write_text(f'names_file = "{other.name}/custom.toml"\n' + voice("v_f", "female"), encoding="utf-8")
        cwd = os.getcwd()
        try:
            os.chdir(tempfile.gettempdir())
            self.assertEqual(app.load_config(self.config)["names"], [("Alice", "female")])
        finally:
            os.chdir(cwd)


class AllocationTests(Fixture):
    def configure(self, female=("Alice", "Cleo"), male=("Bob",), voices=None, body="display_seconds = 0.18\n"):
        write_config(self.paths.config, body, names=names_toml(female, male),
                     voices=voices or (voice("v_f1", "female") + voice("v_f2", "female") + voice("v_m", "male")))
        return app.load_config(self.paths.config)

    def store(self):
        return pane_identity.store(self.paths, lock=app.FileLock, atomic_json=app.atomic_json, private_dir=app.private_dir)

    def allocate(self, config, pane, rng=None):
        env = self.pane_env(pane)
        self.mark_scanned(config, env)
        return pane_identity.resolve(self.paths, config, private_dir=app.private_dir, lock=app.FileLock,
                                     atomic_json=app.atomic_json, error=app.CommsError, env=env, rng=rng)

    def mark_scanned(self, config, env):
        daemon = pane_identity.owner(env)[1]
        def operation(state):
            state["scanned"][daemon] = time.time()  # Never inventory a tuios daemon from tests.
        try:
            self.store().update(operation, config=config)
        except allocation.AllocationError:
            pass  # Invalid/legacy state: the allocation under test reports it.

    def state(self):
        return json.loads((self.paths.state / allocation.STATE_FILE).read_text())

    def test_generations_use_each_base_once_then_numbered_suffixes(self):
        config = self.configure()
        names = [self.allocate(config, f"p{i}")[1]["name"] for i in range(9)]
        self.assertEqual(sorted(names[:3]), ["Alice", "Bob", "Cleo"])
        self.assertEqual(sorted(names[3:6]), ["Alice2", "Bob2", "Cleo2"])
        self.assertEqual(sorted(names[6:]), ["Alice3", "Bob3", "Cleo3"])
        self.assertEqual(len(set(names)), 9)
        self.assertEqual(self.state()["names"]["generation"], 2)
        self.assertEqual(allocation.generated_name("Alice", 10), "Alice11")

    def test_gender_matched_voices_cycle_without_capping_agents(self):
        config = self.configure(female=[f"F{c}" for c in "abcdefgh"], male=["Mo"],
                                voices=voice("v_f1", "female") + voice("v_f2", "female") + voice("v_m", "male"))
        bindings = [self.allocate(config, f"p{i}")[1] for i in range(20)]
        for record in bindings:
            self.assertEqual(record["gender"], record["voice_profile"]["gender"])
            self.assertEqual(record["gender"], "male" if record["base_name"] == "Mo" else "female")
        female = [r["voice_profile_id"] for r in bindings if r["gender"] == "female"]
        # Each eligible profile is used once per matching-gender cycle before reuse.
        for start in range(0, len(female) - 1, 2):
            self.assertEqual(sorted(female[start:start + 2]), ["v_f1", "v_f2"])
        self.assertEqual(len({r["name"] for r in bindings}), 20)

    def test_repeat_lookup_and_restart_return_the_same_committed_assignment(self):
        config = self.configure()
        epoch, first = self.allocate(config, "p")
        again = self.allocate(app.load_config(self.paths.config), "p")
        self.assertEqual(again, (epoch, first))
        before = self.state()
        self.assertEqual(pane_identity.lookup(self.paths, self.pane_env("p")), (epoch, before["bindings"][
            pane_identity.owner(self.pane_env("p"))[0]]))
        self.assertEqual(json.loads(json.dumps(before)), self.state(), "lookups never rewrite state")

    def test_clear_and_clear_all_never_recycle_names(self):
        config = self.configure()
        epoch, alice = self.allocate(config, "p1")
        self.allocate(config, "p2")
        cleared = pane_identity.resolve(self.paths, config, private_dir=app.private_dir, lock=app.FileLock,
                                        atomic_json=app.atomic_json, error=app.CommsError, clear=True,
                                        env=self.pane_env("p1"))
        self.assertEqual(cleared, {"cleared": True, "name": alice["name"]})
        _, third = self.allocate(config, "p1")
        self.assertNotEqual(third["name"], alice["name"])
        remaining = self.state()["names"]["remaining"]
        self.assertEqual(remaining, [])
        result = pane_identity.clear_all(self.paths, private_dir=app.private_dir, lock=app.FileLock,
                                         atomic_json=app.atomic_json, error=app.CommsError)
        self.assertEqual(result, {"cleared": 2})
        state = self.state()
        self.assertEqual((state["epoch"], state["names"]["generation"], state["bindings"]), (epoch, 0, {}))
        _, fourth = self.allocate(config, "p9")
        self.assertTrue(fourth["name"].endswith("2"), "a cleared binding's name stays spent")

    def test_pruned_owners_never_return_names(self):
        config = self.configure()
        _, record = self.allocate(config, "gone")
        key = pane_identity.owner(self.pane_env("gone"))[0]
        daemon = pane_identity.owner(self.pane_env("gone"))[1]
        def age(state):
            state["bindings"][key]["missing_since"] = time.time() - pane_identity.GRACE_SECONDS - 1
            state["scanned"][daemon] = 0
        self.store().update(age)
        with patch.object(pane_identity, "inventory", return_value={"live"}):
            pane_identity.resolve(self.paths, config, private_dir=app.private_dir, lock=app.FileLock,
                                  atomic_json=app.atomic_json, error=app.CommsError, env=self.pane_env("live"))
        state = self.state()
        self.assertNotIn(key, state["bindings"])
        self.assertNotIn(record["base_name"], [n for n, _ in state["names"]["remaining"]])

    def test_concurrent_processes_never_issue_duplicate_names(self):
        config = self.configure(female=[f"F{c}" for c in "abcdefghij"], male=[f"M{c}" for c in "abcdefghij"])
        self.mark_scanned(config, self.pane_env("x"))
        code = ("import json,sys; import agent_comms as a, pane_identity as p; "
                "paths=a.Paths(*map(a.Path, sys.argv[1:4])); config=a.load_config(paths.config); "
                "env={'TUIOS_ENV':'1','TUIOS_SESSION':'test','TUIOS_SOCKET':'/nonexistent/agent-comms-test.sock',"
                "'TUIOS_PANE_ID':sys.argv[4]}; "
                "print(json.dumps(p.resolve(paths,config,private_dir=a.private_dir,lock=a.FileLock,"
                "atomic_json=a.atomic_json,error=a.CommsError,env=env)[1]['name']))")

        def run(index):
            return subprocess.run([sys.executable, "-c", code, str(self.paths.config), str(self.paths.state),
                                   str(self.paths.runtime), f"pane-{index % 30}"],
                                  cwd=ROOT, capture_output=True, text=True, timeout=30)
        with ThreadPoolExecutor(max_workers=10) as pool:
            results = list(pool.map(run, range(45)))
        self.assertTrue(all(r.returncode == 0 for r in results), [r.stderr for r in results])
        by_pane = {}
        for index, result in enumerate(results):
            by_pane.setdefault(index % 30, set()).add(json.loads(result.stdout))
        self.assertTrue(all(len(names) == 1 for names in by_pane.values()), "one owner, one assignment")
        issued = [next(iter(names)) for names in by_pane.values()]
        self.assertEqual(len(issued), len(set(issued)), "different owners never share a name")
        self.assertEqual(len(self.state()["bindings"]), 30)

    def test_failed_commit_leaves_no_partial_name_voice_or_binding(self):
        config = self.configure()
        self.allocate(config, "first")
        before = (self.paths.state / allocation.STATE_FILE).read_bytes()
        with patch.object(app, "atomic_json", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.allocate(config, "second")
        self.assertEqual((self.paths.state / allocation.STATE_FILE).read_bytes(), before)

    def test_corrupt_unsupported_and_inconsistent_state_fail_closed(self):
        config = self.configure()
        self.allocate(config, "p")
        path = self.paths.state / allocation.STATE_FILE
        good = self.state()
        bad_states = {
            "broken json": "Corrupt allocation state",
            json.dumps({**good, "version": 99}): "Unsupported allocation state version",
            json.dumps({**good, "names": {**good["names"], "remaining": [[next(iter(good["bindings"].values()))
                                                                          ["base_name"], "female"]]}}): "still in the name bag",
            json.dumps({**good, "epoch": "nope"}): "missing epoch",
        }
        for content, error in bad_states.items():
            with self.subTest(error=error):
                path.write_text(content)
                with self.assertRaisesRegex(app.CommsError, error) as raised:
                    self.allocate(config, "other")
                self.assertIn("bags reset --confirm", str(raised.exception))
                self.assertEqual(path.read_text(), content, "never silently recreate generation 0")

    def test_catalog_snapshot_finishes_before_config_edits_apply(self):
        config = self.configure(female=("Alice", "Cleo"), male=("Bob",))
        self.allocate(config, "p1")
        config = self.configure(female=("Alice", "Cleo", "Dora"), male=("Bob",))
        names = {self.allocate(config, f"p{i}")[1]["name"] for i in range(2, 4)}
        self.assertNotIn("Dora", names)
        self.assertIn("Dora2", {self.allocate(config, f"p{i}")[1]["name"] for i in range(4, 8)})

    def test_remaining_names_without_voices_fail_before_consuming(self):
        config = self.configure(female=("Alice", "Cleo"), male=("Bob", "Carl"))
        self.allocate(config, "p1")
        before = self.state()
        write_config(self.paths.config, "", names=names_toml(["Alice", "Cleo"]), voices=voice("v_f1", "female"))
        config = app.load_config(self.paths.config)  # The catalog itself is valid...
        # One draw consumed at most one of the two male names, so the generation still holds one.
        with self.assertRaisesRegex(app.CommsError, "no active male voice"):
            self.allocate(config, "p2")
        self.assertEqual(self.state()["names"], before["names"])

    def test_voice_queue_reconciliation_and_snapshots_survive_profile_edits(self):
        config = self.configure(female=[f"F{c}" for c in "abcdef"], male=["Mo"],
                                voices=voice("v_f1", "female", extra="elevenlabs_speed = 0.8\n")
                                + voice("v_f2", "female", "other") + voice("v_m", "male", "other"))
        assignments = [self.allocate(config, f"p{i}")[1] for i in range(3)]
        original = {r["name"]: r["voice_profile"] for r in assignments}
        # Edit, move, and delete source profiles; add a new eligible one.
        write_config(self.paths.config, 'active_groups = ["main"]\n',
                     names=names_toml([f"F{c}" for c in "abcdef"], ["Mo"]),
                     voices=voice("v_f3", "female") + voice("v_m", "male"))
        config = app.load_config(self.paths.config)
        later = [self.allocate(config, f"q{i}")[1] for i in range(4)]
        self.assertTrue(all(r["voice_profile_id"] in ("v_f3", "v_m") for r in later))
        state = self.state()
        self.assertEqual(state["voices"]["female"]["eligible"], ["v_f3"])
        for record in state["bindings"].values():
            if record["name"] in original:
                self.assertEqual(record["voice_profile"], original[record["name"]])
        # Reload with the source profiles gone: existing assignments stay usable.
        for i in range(3):
            self.assertEqual(self.allocate(config, f"p{i}")[1]["voice_profile"], original[assignments[i]["name"]])

    def test_relative_voice_paths_are_absolute_in_snapshots(self):
        config = self.configure(female=("Alice",), male=(),
                                voices=voice("v_f", "female", extra='pocket_tts_voice = "voices/a.wav"\n'))
        _, record = self.allocate(config, "p")
        path = record["voice_profile"]["pocket_tts_voice"]
        self.assertTrue(Path(path).is_absolute())
        self.assertEqual(Path(path), self.paths.config.parent.resolve() / "voices/a.wav")

    def test_reset_starts_new_epoch_and_preserves_history_and_config(self):
        config = self.configure()
        epoch, _ = self.allocate(config, "p")
        for _ in range(3):
            self.allocate(config, f"x{_}")
        (self.paths.state / "history.jsonl").write_text('{"id":"Alice","name":"Alice","msg":"x","created_at":"t"}\n')
        config_text = self.paths.config.read_text()
        result = self.cli("bags", "reset")
        self.assertEqual(result.returncode, 1)
        self.assertIn("--confirm", result.stderr)
        result = self.cli("bags", "reset", "--confirm")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual((report["generation"], report["cleared"], report["migrated"]), (0, 4, False))
        self.assertNotEqual(report["epoch"], epoch)
        self.assertIn("names may now repeat", result.stderr.lower())
        state = self.state()
        self.assertEqual((state["bindings"], state["names"]["generation"], len(state["names"]["remaining"])), ({}, 0, 3))
        self.assertIn("Alice", (self.paths.state / "history.jsonl").read_text())
        self.assertEqual(self.paths.config.read_text(), config_text)
        status = json.loads(self.cli("bags", "status").stdout)
        self.assertEqual((status["epoch"], status["generation"], status["bindings"]), (report["epoch"], 0, 0))

    def test_reset_recovers_corrupt_state_with_backup(self):
        self.configure()
        self.paths.state.mkdir(parents=True)
        (self.paths.state / allocation.STATE_FILE).write_text("broken")
        result = self.cli("bags", "reset", "--confirm")
        self.assertEqual(result.returncode, 0, result.stderr)
        backup = Path(json.loads(result.stdout)["backup"])
        self.assertEqual((backup / allocation.STATE_FILE).read_text(), "broken")
        self.assertEqual(self.state()["names"]["generation"], 0)

    def test_legacy_state_requires_explicit_migration_with_backup(self):
        config = self.configure()
        self.paths.state.mkdir(parents=True)
        legacy = {"version": 1, "bindings": {"k": {"daemon": "d", "pane_id": "p", "id": "ag_1",
                                                   "title_prefix": "[Will] "}}, "scanned": {}}
        (self.paths.state / "pane-identities.json").write_text(json.dumps(legacy))
        (self.paths.state / "bag.json").write_text('{"ids": ["ag_1"], "remaining": []}')
        with self.assertRaisesRegex(app.CommsError, "Legacy agent identity state"):
            self.allocate(config, "p")
        self.assertFalse((self.paths.state / allocation.STATE_FILE).exists())
        status = json.loads(self.cli("bags", "status").stdout)
        self.assertTrue(status["legacy"])
        result = self.cli("bags", "reset", "--confirm")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertTrue(report["migrated"])
        backup = Path(report["backup"])
        self.assertEqual(json.loads((backup / "pane-identities.json").read_text()), legacy)
        self.assertTrue((backup / "bag.json").exists())
        self.assertFalse((self.paths.state / "pane-identities.json").exists())
        self.assertIn("[Will] ", self.state()["retired_title_prefixes"])
        self.assertEqual(self.allocate(config, "p")[1]["name_generation"], 0)

    def test_refill_is_retired_with_guidance(self):
        result = self.cli("context", "--refill")
        self.assertEqual(result.returncode, 1)
        self.assertIn("retired", result.stderr)
        self.assertIn("bags reset --confirm", result.stderr)
        self.assertFalse(self.paths.state.exists())


if __name__ == "__main__":
    unittest.main()
