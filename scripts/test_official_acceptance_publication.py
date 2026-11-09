import copy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts import test_acceptance_reuse as fixtures


class OfficialAcceptancePublicationTest(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.AcceptanceReuseTest('runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.root = fixture.root
        archive = fixture.record['evidenceArchive']
        self.table = 'compatibility/wechat/acceptance-v2.json'
        paths = [self.table, *[row['path'] for row in archive['files']], archive['manifestPath']]
        self.pending = {path: (self.root / path).read_bytes() for path in paths}
        for path in paths:
            (self.root / path).unlink()
        transport = tempfile.TemporaryDirectory()
        self.addCleanup(transport.cleanup)
        self.origin = Path(transport.name) / 'origin.git'
        self.run_git('init', '--bare', str(self.origin), cwd=self.origin.parent)
        self.run_git('init', '-b', 'main')
        self.run_git('config', 'user.name', 'Test')
        self.run_git('config', 'user.email', 'test@example.invalid')
        self.run_git('add', '.')
        self.run_git('commit', '-m', 'Initial source')
        self.run_git('remote', 'add', 'origin', str(self.origin))
        self.run_git('push', '-u', 'origin', 'main')
        self.head = self.run_git('rev-parse', 'HEAD')
        for path, data in self.pending.items():
            (self.root / path).write_bytes(data)
        self.registered = {
            'status': 'REGISTERED', 'acceptancePath': self.table,
            'recordId': fixture.record['recordId'],
            'evidencePaths': paths[1:],
            'evidenceManifest': {'path': archive['manifestPath'], 'sha256': archive['manifestSha256']},
        }

    def run_git(self, *args, cwd=None):
        return subprocess.check_output(['git', *args], cwd=cwd or self.root,
                                       text=True, stderr=subprocess.PIPE).strip()

    def publish(self, registered=None):
        from scripts.ci.register_official_acceptance import commit_registered_acceptance
        return commit_registered_acceptance(self.root, 'wechat', registered or self.registered, self.head)

    def test_publishes_only_verified_table_and_evidence_to_main(self):
        (self.root / 'unrelated.txt').write_text('do not commit')
        self.assertTrue(self.publish())
        paths = set(self.run_git('diff', '--name-only', self.head, 'HEAD').splitlines())
        self.assertEqual(set(self.pending), paths)
        self.assertEqual(self.run_git('rev-parse', 'HEAD'),
                         self.run_git('--git-dir=' + str(self.origin), 'rev-parse', 'refs/heads/main'))
        self.assertEqual('?? unrelated.txt', self.run_git('status', '--short'))

    def test_stale_main_does_not_commit_or_overwrite_remote(self):
        other = self.origin.parent / 'other'
        self.run_git('clone', '--branch', 'main', str(self.origin), str(other), cwd=self.origin.parent)
        self.run_git('config', 'user.name', 'Other', cwd=other)
        self.run_git('config', 'user.email', 'other@example.invalid', cwd=other)
        (other / 'new-source.txt').write_text('new source')
        self.run_git('add', '.', cwd=other)
        self.run_git('commit', '-m', 'Concurrent source update', cwd=other)
        self.run_git('push', 'origin', 'main', cwd=other)
        remote_head = self.run_git('rev-parse', 'HEAD', cwd=other)
        self.assertFalse(self.publish())
        self.assertEqual(self.head, self.run_git('rev-parse', 'HEAD'))
        self.assertEqual(remote_head, self.run_git('--git-dir=' + str(self.origin), 'rev-parse', 'refs/heads/main'))

    def test_unrelated_staged_file_or_unverified_path_cannot_be_published(self):
        (self.root / 'unrelated.txt').write_text('do not commit')
        self.run_git('add', 'unrelated.txt')
        with self.assertRaisesRegex(ValueError, 'empty staging'):
            self.publish()
        self.run_git('reset', '--', 'unrelated.txt')
        forged = copy.deepcopy(self.registered)
        forged['evidencePaths'].append('unrelated.txt')
        with self.assertRaisesRegex(ValueError, 'verified evidence archive'):
            self.publish(forged)
        self.assertEqual(self.head, self.run_git('rev-parse', 'HEAD'))
