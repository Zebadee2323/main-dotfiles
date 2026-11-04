import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('snapshot', Path(__file__).with_name('review_snapshot.py'))
snapshot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(snapshot)


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'repo'
        self.root.mkdir()
        self.git('init', '-q')
        self.git('config', 'user.name', 'Test')
        self.git('config', 'user.email', 'test@example.invalid')
        (self.root / 'file').write_bytes(b'original\x00bytes')
        self.git('add', '.')
        self.git('commit', '-qm', 'base')
        self.base = self.git('rev-parse', 'HEAD').decode().strip()

    def git(self, *args):
        return subprocess.check_output(['git', '-C', str(self.root), *args], stderr=subprocess.PIPE)

    def test_mutation_and_index_detected(self):
        before = snapshot.capture(self.root, self.base)
        self.assertEqual(before, snapshot.capture(self.root, self.base))
        (self.root / 'new').write_text('new')
        after = snapshot.capture(self.root, self.base)
        self.assertNotEqual(before, after)
        self.git('add', 'new')
        self.assertNotEqual(after, snapshot.capture(self.root, self.base))

    def test_commit_matches_modes_symlinks_deletion_binary(self):
        (self.root / 'file').unlink()
        (self.root / 'executable').write_bytes(b'\x00\xff')
        (self.root / 'executable').chmod(0o755)
        (self.root / 'link').symlink_to('executable')
        approved = snapshot.capture(self.root, self.base)
        self.git('add', '-A')
        self.git('commit', '-qm', 'change')
        self.assertTrue(snapshot.verify_commit(self.root, approved, 'HEAD'))
        (self.root / 'executable').write_bytes(b'different')
        self.git('add', '-A')
        self.git('commit', '-qm', 'unexpected')
        self.assertFalse(snapshot.verify_commit(self.root, approved, 'HEAD'))

    def test_ignored_output_excluded(self):
        (self.root / '.gitignore').write_text('output\n')
        before = snapshot.capture(self.root, self.base)
        (self.root / 'output').write_text('generated')
        self.assertEqual(before, snapshot.capture(self.root, self.base))

    def test_cli_capture_verify_and_commit(self):
        import sys
        tool = str(Path(__file__).with_name('review_snapshot.py'))
        manifest = str(Path(self.tmp.name) / 'approval.json')
        def run(action, *extra):
            return subprocess.run([sys.executable, tool, action, '--root', str(self.root),
                                   '--manifest', manifest, *extra], capture_output=True)
        self.assertEqual(run('capture', '--base', self.base).returncode, 0)
        self.assertEqual(run('verify').returncode, 0)
        self.assertEqual(run('verify-commit').returncode, 0)
        (self.root / 'file').write_text('changed')
        self.assertEqual(run('verify').returncode, 1)
        self.assertEqual(run('capture').returncode, 2)

    def test_active_rebase_rejected(self):
        (self.root / '.git/rebase-merge').mkdir()
        with self.assertRaises(ValueError):
            snapshot.capture(self.root, self.base)


if __name__ == '__main__':
    unittest.main()
