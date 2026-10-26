import os
from pathlib import Path
import subprocess
import sys
import time
from unittest.mock import patch

import agent_comms as app
from test_agent_comms import Fixture, ROOT


class AudioWorkspaceTests(Fixture):
    def setUp(self):
        super().setUp()
        self.default_root = app.AudioWorkspace.root
        self.root = app.private_dir(Path(self.temp.name) / "audio")
        self.root_patch = patch.object(app.AudioWorkspace, "root", return_value=self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)

    def test_default_root_uses_os_temp_and_private_user_directory(self):
        with patch.object(app.tempfile, "gettempdir", return_value=self.temp.name):
            root = self.default_root()
        self.assertEqual(root.parent.parent, Path(self.temp.name))
        self.assertTrue(root.parent.name.startswith("agent-comms-"))
        self.assertEqual(root.name, "audio")
        if os.name != "nt":
            self.assertEqual(root.parent.stat().st_mode & 0o777, 0o700)

    def age(self, path):
        old = time.time() - app.AudioWorkspace.STALE_SECONDS - 60
        os.utime(path, (old, old))

    def test_workspace_is_private_and_removed_on_close(self):
        workspace = app.AudioWorkspace()
        self.assertEqual(workspace.path.parent, self.root)
        (workspace.path / "speech-test.wav").write_bytes(b"audio")
        if os.name != "nt":
            self.assertEqual(workspace.path.stat().st_mode & 0o777, 0o700)
        workspace.close()
        self.assertFalse(workspace.path.exists())

    def test_prunes_only_stale_workspaces_not_recent_or_unrelated_entries(self):
        stale = self.root / "speech-orphan"
        recent = self.root / "speech-recent"
        unrelated = self.root / "unrelated"
        for path in (stale, recent, unrelated):
            path.mkdir()
            (path / "speech-test.wav").write_bytes(b"audio")
        self.age(stale)
        self.age(unrelated)
        workspace = app.AudioWorkspace()
        self.addCleanup(workspace.close)
        self.assertFalse(stale.exists())
        self.assertTrue(recent.exists())
        self.assertTrue(unrelated.exists())

    def test_symlink_is_never_followed_or_removed(self):
        if os.name == "nt":
            self.skipTest("Windows symlink creation requires privileges")
        target = Path(self.temp.name) / "external"
        target.mkdir()
        (target / "keep.wav").write_bytes(b"keep")
        link = self.root / "speech-symlink"
        link.symlink_to(target, target_is_directory=True)
        self.age(target)
        workspace = app.AudioWorkspace()
        self.addCleanup(workspace.close)
        self.assertTrue(link.is_symlink())
        self.assertTrue((target / "keep.wav").exists())

    def test_live_workspace_survives_age_then_is_pruned_after_hard_kill(self):
        script = (
            "from pathlib import Path; import sys,time; import agent_comms as a; "
            "a.AudioWorkspace.root=staticmethod(lambda: Path(sys.argv[1])); "
            "w=a.AudioWorkspace(); print(w.path,flush=True); time.sleep(60)"
        )
        process = subprocess.Popen([sys.executable, "-c", script, str(self.root)], cwd=ROOT,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            path = Path(process.stdout.readline().strip())
            self.assertEqual(path.parent, self.root)
            self.age(path)
            workspace = app.AudioWorkspace()
            workspace.close()
            self.assertTrue(path.exists(), "Active audio must survive stale-file cleanup")
            process.kill()
            process.wait(timeout=5)
            workspace = app.AudioWorkspace()
            workspace.close()
            self.assertFalse(path.exists(), "Orphan should be reclaimed after its process dies")
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)
