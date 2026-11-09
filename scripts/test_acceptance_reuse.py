import copy
import hashlib
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import test_semantic_acceptance as fixtures


class AcceptanceReuseTest(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.SemanticAcceptanceTest('runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.root = fixture.root
        self.record = copy.deepcopy(fixture.record)
        script = self.root / 'scripts/ci/probe.py'
        script.parent.mkdir(parents=True)
        script.write_text('runtime implementation')
        from scripts.ci.acceptance_reuse import runtime_validation_sha256
        self.runtime_sha = runtime_validation_sha256(self.root)
        self.candidate = {
            'appId': 'wechat', 'status': 'PREPARED',
            'identity': self.record['identity'], 'apkFiles': self.record['apkFiles'],
            'expectedPlan': self.record['completePlan'],
            'expectedExecutionSha256': self.record['executionSha256'],
            'recognitionSha256': self.record['recognitionSha256'],
        }
        self.static = self.record['staticReport']
        self.save_record()

    def save_record(self):
        archive = self.record['evidenceArchive']
        for index, item in enumerate(archive['files']):
            path = self.root / item['path']
            path.parent.mkdir(parents=True, exist_ok=True)
            value = ({'status': 'RUNTIME_SMOKE_VERIFIED',
                      'runtimeValidationSha256': self.runtime_sha} if index == 2 else {'raw': index})
            path.write_text(json.dumps(value))
            item['sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
        refs = self.record['rawStageEvidence']
        for key, item in zip(('static', 'regression', 'runtime'), archive['files']):
            refs[key]['sha256'] = item['sha256']
        refs['files'][0]['sha256'] = archive['files'][3]['sha256']
        manifest = self.root / archive['manifestPath']
        manifest.write_text(json.dumps({'schemaVersion': 1, 'recordId': self.record['recordId'],
                                        'files': archive['files']}))
        archive['manifestSha256'] = hashlib.sha256(manifest.read_bytes()).hexdigest()
        (self.root / 'compatibility/wechat/acceptance-v2.json').write_text(json.dumps(
            {'schemaVersion': 2, 'appId': 'wechat', 'records': [self.record]}))

    def lookup(self):
        from scripts.ci.acceptance_reuse import find_reusable_acceptance
        return find_reusable_acceptance(self.root, self.candidate, self.static)

    def test_exact_verified_candidate_reuses_archived_acceptance(self):
        self.assertEqual(self.lookup()['recordId'], self.record['recordId'])

    def test_changed_apk_plan_or_execution_does_not_reuse(self):
        for field in ('identity', 'apkFiles', 'expectedPlan', 'expectedExecutionSha256', 'recognitionSha256'):
            with self.subTest(field=field):
                original = self.candidate[field]
                self.candidate[field] = {} if isinstance(original, dict) else [] if isinstance(original, list) else '0' * 64
                self.assertIsNone(self.lookup())
                self.candidate[field] = original
        changed = copy.deepcopy(self.static)
        changed['executionIdentity']['sourceTreeSha256'] = '0' * 64
        from scripts.ci.acceptance_reuse import find_reusable_acceptance
        self.assertIsNone(find_reusable_acceptance(self.root, self.candidate, changed))

    def test_runtime_changes_invalidate_but_acceptance_commits_do_not(self):
        (self.root / 'compatibility/notes.md').write_text('new acceptance')
        self.assertIsNotNone(self.lookup())
        (self.root / 'scripts/ci/probe.py').write_text('new runtime')
        self.assertIsNone(self.lookup())

    def test_corrupt_or_missing_evidence_cannot_be_reused(self):
        path = self.root / self.record['evidenceArchive']['files'][3]['path']
        path.write_text('tampered')
        with self.assertRaises(ValueError):
            self.lookup()
        path.unlink()
        with self.assertRaises((ValueError, FileNotFoundError)):
            self.lookup()

    def test_old_runtime_without_fingerprint_is_not_reusable(self):
        self.runtime_sha = None
        self.save_record()
        self.assertIsNone(self.lookup())

    def test_missing_table_is_a_cache_miss(self):
        (self.root / 'compatibility/wechat/acceptance-v2.json').unlink()
        self.assertIsNone(self.lookup())

    def test_pipeline_report_binds_current_runtime_implementation(self):
        from scripts.ci.acceptance_reuse import runtime_validation_sha256
        from scripts.ci.redroid_app_smoke import pipeline_report
        report = pipeline_report({'status': 'ACCEPTANCE_UNAVAILABLE'}, self.candidate)
        self.assertEqual(report['runtimeValidationSha256'], runtime_validation_sha256(Path(__file__).resolve().parents[1]))

    def test_failed_current_stage_revalidation_falls_back_to_full_runtime(self):
        from scripts.ci.acceptance_reuse import main
        directory = self.root / 'candidate'
        (directory / 'regression').mkdir(parents=True)
        static_path = directory / 'static.json'
        static_path.write_text(json.dumps(self.static))
        candidate = dict(self.candidate, staticReport='static.json',
                         staticReportSha256=hashlib.sha256(static_path.read_bytes()).hexdigest())
        (directory / 'candidate-report.json').write_text(json.dumps(candidate))
        (directory / 'regression/results.json').write_text(json.dumps([{'status': 'PASSED'}]))
        output = self.root / 'outputs'
        report = self.root / 'summary.md'
        with patch('sys.argv', ['acceptance_reuse', '--directory', str(directory),
                               '--repository', str(self.root), '--github-output', str(output),
                               '--report', str(report)]), patch(
                'scripts.ci.runtime_task._semantic_stage_context', side_effect=ValueError('stale baseline')):
            main()
        self.assertEqual('acceptance_reused=false\n', output.read_text())
        self.assertIn('stale baseline', report.read_text())
