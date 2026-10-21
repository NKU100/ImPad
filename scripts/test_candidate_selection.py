import hashlib
import io
import json
from contextlib import redirect_stderr
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.ci.app_policy import get_policy
from scripts.ci.candidate_selection import (
    main, module_source_hashes, prepare_candidate, prepare_candidate_from_adaptation_report,
    prepare_module, source_tree_sha256, write_local_runtime_assets,
)


class CandidateSelectionTest(unittest.TestCase):
    @staticmethod
    def _wecom_identity(digest):
        return {'packageName': 'com.tencent.wework', 'versionName': '5.0.8',
                'versionCode': 70675, 'abi': 'arm64-v8a', 'apkSha256': digest,
                'signerSha256': get_policy('wecom').signer_sha256}

    @staticmethod
    def _wecom_static_report(identity, apk):
        targets = {
            'device_type': {'descriptor': 'Lcom/tencent/wework/foundation/logic/Application;->getDeviceTypeFromLocal()I',
                            'shape': {'parameters': [], 'returnType': 'int', 'isStatic': False}},
            'app_pad': {'descriptor': 'Lcom/tencent/wework/foundation/logic/Application;->isAndroidPad()Z',
                        'shape': {'parameters': [], 'returnType': 'boolean', 'isStatic': True}},
            'service_pad': {'descriptor': 'Lcom/tencent/wework/foundation/impl/WeworkServiceImpl;->isAndroidPad()Z',
                            'shape': {'parameters': [], 'returnType': 'boolean', 'isStatic': False}},
        }
        catalog_sha = 'd' * 64
        return {'appId': 'wecom', 'analysisScope': 'STATIC_ONLY', 'identity': identity,
                'inputHashes': [{'path': str(apk.resolve()), 'sha256': identity['apkSha256']}],
                'ruleCatalogSha256': catalog_sha,
                'ruleCatalog': {'schemaVersion': 1, 'appId': 'wecom',
                                'ruleCatalogSha256': catalog_sha,
                                'definitions': [{'ruleId': 'wecom_local_001',
                                                 'strategyId': 'wecom_local_tablet_001',
                                                 'requiredTargetIds': ['device_type', 'app_pad', 'service_pad']}],
                                'bindings': [{'ruleId': 'wecom_local_001', 'min': 70675, 'max': None}],
                                'strategyIds': ['wecom_local_tablet_001']},
                'routeCandidates': ['wecom_local_001'],
                'selection': {'status': 'SELECTED', 'selectedRuleId': 'wecom_local_001',
                              'strategyId': 'wecom_local_tablet_001', 'targets': targets}}

    def _assert_wecom_candidate(self, directory, candidate, identity):
        self.assertEqual(1, candidate['schemaVersion'])
        self.assertEqual('wecom', candidate['appId'])
        self.assertEqual('PREPARED', candidate['status'])
        self.assertEqual(identity, candidate['identity'])
        self.assertEqual('UNIQUE', candidate['expectedPlan']['selection'])
        self.assertEqual({'device_type', 'app_pad', 'service_pad'},
                         set(candidate['expectedPlan']['targets']))
        self.assertEqual(['wecom_local_001'], candidate['route']['candidates'])
        static_path = directory / candidate['staticReport']
        static = json.loads(static_path.read_text())
        self.assertEqual(candidate['staticReportSha256'],
                         hashlib.sha256(static_path.read_bytes()).hexdigest())
        self.assertEqual(static['inputHashes'][0]['path'], candidate['apkFiles'][0]['path'])
        for row in candidate['apkFiles']:
            staged = directory / row['path']
            self.assertEqual(row['sha256'], hashlib.sha256(staged.read_bytes()).hexdigest())
        from scripts.ci.acceptance import validate_expected_plan
        validate_expected_plan(candidate['expectedPlan'], static['ruleCatalog'], identity['versionCode'])

    def test_candidate_cli_requires_explicit_app_and_rejects_targets_input(self):
        error = io.StringIO()
        with redirect_stderr(error), patch('sys.argv', ['candidate_selection', '--directory', '/tmp/unused']):
            with self.assertRaises(SystemExit) as failure:
                main()
        self.assertEqual(2, failure.exception.code)
        self.assertIn('--app-id', error.getvalue())

        error = io.StringIO()
        with redirect_stderr(error), patch('sys.argv', [
            'candidate_selection', '--app-id', 'wechat', '--targets', 'compatibility/wechat/targets.json',
            '--directory', '/tmp/unused',
        ]):
            with self.assertRaises(SystemExit) as failure:
                main()
        self.assertEqual(2, failure.exception.code)
        self.assertIn('unrecognized arguments: --targets', error.getvalue())

    def test_official_candidate_uses_shared_checker_and_exact_apk_bytes(self):
        body = b'official WeCom 5.0.8 APK'
        digest = hashlib.sha256(body).hexdigest()
        source_url = 'https://dldir1.qq.com/wework/work_weixin/WeCom_android_5.0.8.70675_arm64_100001.apk'
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            apk = root / 'downloaded.apk'
            apk.write_bytes(body)
            identity = self._wecom_identity(digest)
            static = self._wecom_static_report(identity, apk)
            with patch('sys.argv', ['candidate_selection', '--app-id', 'wecom',
                                    '--apk-url', source_url, '--directory', str(root / 'prepared')]), \
                    patch('scripts.ci.candidate_selection.remote_metadata', return_value={}), \
                    patch('scripts.ci.candidate_selection.download_candidate',
                          return_value=(apk, digest, {}, {})), \
                    patch('scripts.ci.candidate_selection._run_rules_checker', return_value=static), \
                    patch('scripts.ci.apps.qq.runtime_probe.manifest_expectation',
                          side_effect=AssertionError('QQ-only evidence must not be applied to WeCom')):
                main()
            candidate = json.loads((root / 'prepared/candidate-report.json').read_text())
            self._assert_wecom_candidate(root / 'prepared', candidate, identity)
            self.assertTrue((root / 'prepared' / f'wecom-{digest}.apk').is_file())

    def test_uploaded_adaptation_artifact_is_rechecked_in_place(self):
        body = b'uploaded official WeCom candidate'
        digest = hashlib.sha256(body).hexdigest()
        source_url = 'https://dldir1.qq.com/wework/work_weixin/WeCom_android_5.0.8.70675_arm64_100001.apk'
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            apk = root / f'wecom-{digest}.apk'
            apk.write_bytes(body)
            identity = self._wecom_identity(digest)
            report = root / 'candidate-report.json'
            report.write_text(json.dumps({'appId': 'wecom', 'status': 'STATIC_VERIFIED_PENDING_RUNTIME',
                                          'identity': identity, 'sourceUrl': source_url}))
            static = self._wecom_static_report(identity, apk)
            with patch('scripts.ci.candidate_selection._run_rules_checker', return_value=static) as checker:
                candidate = prepare_candidate_from_adaptation_report('wecom', report, root)
            self._assert_wecom_candidate(root, candidate, identity)
            checker.assert_called_once()
            self.assertEqual(apk.resolve(), checker.call_args.args[1].resolve())

    def test_runtime_assets_contain_only_xposed_scope(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            candidate = {'identity': {'packageName': get_policy('wechat').package_name}}
            write_local_runtime_assets(candidate, 'wechat', root)
            self.assertEqual('com.tencent.mm\n',
                             (root / 'resources/META-INF/xposed/scope.list').read_text())
            self.assertFalse((root / 'assets/compatibility-local').exists())

    def test_module_identity_rejects_apk_without_build_fingerprint(self):
        with tempfile.TemporaryDirectory() as temporary:
            module = Path(temporary) / 'old-module.apk'
            module.write_bytes(b'APK without build source fingerprint')
            with self.assertRaisesRegex(ValueError, 'build-time fingerprint'):
                prepare_module(module, {'appId': 'qq', 'routeCandidates': [],
                                        'executionIdentity': {}}, Path.cwd())

    def test_module_source_digest_includes_gradle_properties(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'gradle.properties').write_bytes(b'android.foo=true\n')
            lock = root / 'gradle/dexkit-lock.json'
            lock.parent.mkdir()
            lock.write_bytes(b'{}\n')
            before = module_source_hashes(root)
            before_digest = source_tree_sha256(root)
            (root / 'gradle.properties').write_bytes(b'android.foo=false\n')
            after = module_source_hashes(root)
        self.assertEqual(sorted(row['path'] for row in after), [row['path'] for row in after])
        self.assertNotEqual(before_digest, source_tree_sha256(root))
        self.assertNotEqual(before, after)

    def test_module_source_fingerprint_includes_collector_but_excludes_its_test_harness(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            probe = root / 'scripts/ci/apps/qq/probe'
            probe.mkdir(parents=True)
            (probe / 'collector.c').write_text('int production_collector(void) { return 1; }\n')
            (probe / 'collector_owner_test.c').write_text('int test_harness(void) { return 1; }\n')
            other_probe = root / 'scripts/ci/apps/wechat/probe'
            other_probe.mkdir(parents=True)
            (other_probe / 'parser_test.c').write_text('int production_parser(void) { return 2; }\n')

            sources = module_source_hashes(root)

        paths = {row['path'] for row in sources}
        self.assertIn('scripts/ci/apps/qq/probe/collector.c', paths)
        self.assertIn('scripts/ci/apps/wechat/probe/parser_test.c', paths)
        self.assertNotIn('scripts/ci/apps/qq/probe/collector_owner_test.c', paths)


if __name__ == '__main__':
    unittest.main()
