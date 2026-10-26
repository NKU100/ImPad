import hashlib
import io
import json
from contextlib import redirect_stderr
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from scripts.ci.app_policy import get_policy
from scripts.ci.candidate_selection import (
    dependency_lock_sha256, main, module_source_hashes, prepare_candidate,
    prepare_candidate_from_adaptation_report, prepare_module, route_configuration_sha256,
    source_tree_sha256, write_local_runtime_assets,
)


class CandidateSelectionTest(unittest.TestCase):
    def test_automatic_discovery_keeps_official_download_policy(self):
        from types import SimpleNamespace
        from scripts.ci.candidate_selection import prepare_discovered_candidate
        url = 'https://dldir1v6.qq.com/weixin/android/weixin8079.apk'
        strategy = SimpleNamespace(discover=lambda policy: SimpleNamespace(url=url))
        with patch('scripts.ci.candidate_selection.prepare_candidate', return_value={}) as prepare, \
             patch('scripts.ci.candidate_selection.importlib.import_module', return_value=strategy):
            prepare_discovered_candidate('wechat', Path('work/test-discovery'))
        prepare.assert_called_once_with('wechat', url, Path('work/test-discovery'), allow_external=False)

    def test_external_xapk_passes_every_original_apk_to_shared_checker(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            bundle = root / 'download.apk'
            with zipfile.ZipFile(bundle, 'w') as archive:
                archive.writestr('com.tencent.wework.apk', b'base')
                archive.writestr('config.arm64_v8a.apk', b'native')
                archive.writestr('manifest.json', json.dumps({'package_name': 'com.tencent.wework'}))
            digest = hashlib.sha256(b'base').hexdigest()
            identity = self._wecom_identity(digest)

            def check(app_id, apk, report_path, repository=None, abi_splits=()):
                self.assertEqual([b'native'], [path.read_bytes() for path in abi_splits])
                report = self._wecom_static_report(identity, apk)
                report['inputHashes'] += [{'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
                                          for path in abi_splits]
                return report

            url = 'https://mirror.example/download?id=123'
            with patch('scripts.ci.candidate_selection.remote_metadata', return_value={}), \
                 patch('scripts.ci.candidate_selection.download_candidate',
                       return_value=(bundle, hashlib.sha256(bundle.read_bytes()).hexdigest(), False, {})), \
                 patch('scripts.ci.candidate_selection._run_rules_checker', side_effect=check):
                candidate = prepare_candidate('wecom', url, root / 'prepared')
            self.assertEqual(2, len(candidate['apkFiles']))
            self.assertEqual(hashlib.sha256(bundle.read_bytes()).hexdigest(), candidate['sourceArchiveSha256'])
            self._assert_wecom_candidate(root / 'prepared', candidate, identity)

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

    @staticmethod
    def _fingerprinted_module_fixture(root, mutate_fingerprint=None):
        keep_rule = root / 'app/src/release/keepRules/impad-module-entry.keep'
        keep_rule.parent.mkdir(parents=True)
        keep_rule.write_text('-keep class example.Module { public <init>(); }\n')
        for relative, content in {
            'gradle/dexkit-lock.json': b'{"lock":1}\n',
            'gradle/libs.versions.toml': b'[versions]\n',
            'gradle/wrapper/gradle-wrapper.properties': b'distributionUrl=example\n',
        }.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)

        sources = module_source_hashes(root)
        fingerprint = {
            'schemaVersion': 1,
            'moduleSources': sources,
            'sourceTreeSha256': source_tree_sha256(root),
            'routeConfigurationSha256': route_configuration_sha256(sources),
            'dependencyLockSha256': dependency_lock_sha256(sources),
        }
        if mutate_fingerprint is not None:
            mutate_fingerprint(fingerprint)
        raw_fingerprint = (json.dumps(fingerprint, indent=2) + '\n').encode()
        module_apk = root / 'module.apk'
        with zipfile.ZipFile(module_apk, 'w') as archive:
            archive.writestr('assets/runtime-identity.json', raw_fingerprint)
        catalog_sha = 'd' * 64
        static_report = {
            'appId': 'qq',
            'routeCandidates': [],
            'ruleCatalogSha256': catalog_sha,
            'executionIdentity': {
                'ruleCatalogSha256': catalog_sha,
                'fingerprintFileSha256': hashlib.sha256(raw_fingerprint).hexdigest(),
                'sourceTreeSha256': fingerprint['sourceTreeSha256'],
                'routeConfigurationSha256': fingerprint['routeConfigurationSha256'],
                'dependencyLockSha256': fingerprint['dependencyLockSha256'],
            },
        }
        return keep_rule, module_apk, static_report, fingerprint

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

    def test_production_module_enumerator_matches_unique_apk_inputs_including_release_keep_rules(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            keep_rule, module_apk, static_report, fingerprint = self._fingerprinted_module_fixture(root)
            sources = module_source_hashes(root)
            source_paths = [row['path'] for row in sources]

            self.assertEqual(len(source_paths), len(set(source_paths)))
            self.assertEqual(
                [row['path'] for row in fingerprint['moduleSources']],
                source_paths,
            )
            keep_rows = [row for row in sources
                         if row['path'] == 'app/src/release/keepRules/impad-module-entry.keep']
            self.assertEqual(1, len(keep_rows))
            self.assertEqual(hashlib.sha256(keep_rule.read_bytes()).hexdigest(), keep_rows[0]['sha256'])
            with patch('scripts.ci.candidate_selection.subprocess.check_output', return_value='0' * 40 + '\n'):
                prepared = prepare_module(module_apk, static_report, root)
            self.assertEqual(fingerprint['moduleSources'], prepared['fingerprint']['moduleSources'])

    def test_release_keep_rule_content_change_invalidates_prepared_module_fingerprint(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            keep_rule, module_apk, static_report, _ = self._fingerprinted_module_fixture(root)
            keep_rule.write_text(keep_rule.read_text() + '# changed\n')
            with self.assertRaisesRegex(ValueError, 'fingerprint does not match current source'):
                prepare_module(module_apk, static_report, root)

    def test_missing_release_keep_rule_invalidates_prepared_module_fingerprint(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            keep_rule, module_apk, static_report, _ = self._fingerprinted_module_fixture(root)
            keep_rule.unlink()
            with self.assertRaisesRegex(ValueError, 'fingerprint does not match current source'):
                prepare_module(module_apk, static_report, root)

    def test_tampered_release_keep_rule_hash_in_apk_fingerprint_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            def tamper_keep_hash(fingerprint):
                keep = next(row for row in fingerprint['moduleSources']
                            if row['path'] == 'app/src/release/keepRules/impad-module-entry.keep')
                keep['sha256'] = '0' * 64
                payload = ''.join(
                    f"{row['path']}\n{row['sha256']}\n"
                    for row in sorted(fingerprint['moduleSources'], key=lambda item: item['path'])
                )
                fingerprint['sourceTreeSha256'] = hashlib.sha256(payload.encode()).hexdigest()

            _, module_apk, static_report, _ = self._fingerprinted_module_fixture(root, tamper_keep_hash)
            with self.assertRaisesRegex(ValueError, 'fingerprint does not match current source'):
                prepare_module(module_apk, static_report, root)


if __name__ == '__main__':
    unittest.main()
