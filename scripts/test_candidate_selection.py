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
    _candidate_from_static, dependency_lock_sha256, main, module_source_hashes, prepare_candidate,
    prepare_candidate_from_adaptation_report, prepare_module, verify_module_identity, route_configuration_sha256,
    source_tree_sha256, write_local_runtime_assets,
)


class CandidateSelectionTest(unittest.TestCase):
    @staticmethod
    def _semantic_report(app_id, apk, status='SELECTED'):
        from scripts.ci.semantic_migration import load_migration_bundle
        from scripts.test_semantic_migration import SemanticMigrationTest
        root = Path(__file__).resolve().parents[1]
        profile = next(row for row in load_migration_bundle(root).profiles if row['appId'] == app_id)
        report = SemanticMigrationTest().report(profile)
        digest = hashlib.sha256(Path(apk).read_bytes()).hexdigest()
        report['identity']['apkSha256'] = digest
        report['inputHashes'][0].update(path=str(Path(apk).resolve()), sha256=digest)
        report['snapshot']['archives'][0]['containerSha256'] = digest
        if status == 'NO_MATCH':
            query = report['recognizers'][0]
            query.update(status='NO_MATCH', plans=[])
            report['selection'].update(status='NO_MATCH', plan=None, executionSha256=None,
                                       evidenceByRecognizer={}, alternatives=[])
        elif status == 'CONFLICT':
            import copy
            from scripts.ci.semantic_protocol import plan_execution_sha256
            recognizer = report['recognizers'][0]
            original = recognizer['plans'][0]
            alternate = copy.deepcopy(original)
            alternate['plan']['hooks'][0]['descriptor'] = alternate['plan']['hooks'][0]['descriptor'].replace(
                'getDeviceTypeFromLocal', 'getDeviceTypeFromLocalAlternate')
            alternate['executionSha256'] = plan_execution_sha256(alternate['plan'])
            alternate['evidence'] = ['alternate structural match']
            recognizer['plans'].append(alternate)
            alternatives = [{'plan': row['plan'], 'executionSha256': row['executionSha256'],
                'evidenceByRecognizer': {recognizer['recognizerId']: row['evidence']}}
                for row in recognizer['plans']]
            report['selection'].update(status='CONFLICT', plan=None, executionSha256=None,
                evidenceByRecognizer={}, alternatives=sorted(alternatives, key=lambda row: row['executionSha256']))
        elif status in {'INCOMPLETE', 'EVALUATION_ERROR', 'UNSUPPORTED_INPUT'}:
            query = report['recognizers'][0]
            query.update(status=status, plans=[], reason='bounded diagnostic' if status == 'INCOMPLETE' else None,
                         diagnostics=[] if status not in {'EVALUATION_ERROR', 'UNSUPPORTED_INPUT'} else ['bounded diagnostic'])
            report['selection'].update(status=status, plan=None, executionSha256=None,
                evidenceByRecognizer={}, alternatives=[],
                diagnostics={query['recognizerId']: ['bounded diagnostic']} if status in {'EVALUATION_ERROR', 'UNSUPPORTED_INPUT'} else {},
                incompleteReasons={query['recognizerId']: 'bounded diagnostic'} if status == 'INCOMPLETE' else {})
        return report

    def test_registered_mode_prefers_v2_and_rechecks_saved_identity_plan_and_inputs(self):
        from scripts.test_semantic_acceptance import SemanticAcceptanceTest
        fixture = SemanticAcceptanceTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        record = fixture.record
        table_path = fixture.root / 'compatibility/wechat/acceptance-v2.json'
        table_path.write_text(json.dumps({'schemaVersion': 2, 'appId': 'wechat',
                                          'records': [record]}))
        output = fixture.root / 'registered-output'
        calls = []

        def recheck(app_id, source_url, directory, *, allow_external=True):
            calls.append((app_id, source_url, allow_external))
            directory = Path(directory)
            apk_files = []
            for index, saved in enumerate(record['apkFiles']):
                relative = f'apk-inputs/input-{index}.apk'
                (directory / relative).parent.mkdir(parents=True, exist_ok=True)
                (directory / relative).write_bytes(f'synthetic rechecked input {index}'.encode())
                apk_files.append({'path': relative, 'role': saved['role'], 'sha256': saved['sha256']})
            return {
                'schemaVersion': 2, 'appId': app_id, 'status': 'PREPARED',
                'identity': record['identity'], 'sourceUrl': source_url,
                'apkFiles': apk_files,
                'expectedPlan': record['completePlan'],
                'expectedExecutionSha256': record['executionSha256'],
                'sourceArchiveSha256': record['source'].get('archiveSha256'),
            }

        def prepare_output_directory(candidate, source_directory, destination_directory):
            Path(destination_directory).mkdir(parents=True, exist_ok=True)

        with patch('scripts.ci.candidate_selection.Path.cwd', return_value=fixture.root), \
             patch('scripts.ci.candidate_selection.prepare_candidate', side_effect=recheck), \
             patch('scripts.ci.candidate_selection._copy_registered_artifact',
                   side_effect=prepare_output_directory), \
             patch('scripts.ci.candidate_selection.shutil.copy2'), \
             patch('sys.argv', ['candidate_selection', '--app-id', 'wechat', '--mode', 'registered',
                                '--directory', str(output)]):
            main()

        candidate = json.loads((output / 'candidate-report.json').read_text())
        self.assertEqual(1, len(calls), 'each registered row must be prepared only once')
        self.assertTrue(all(call[1] == record['source']['url'] for call in calls))
        self.assertFalse(any(call[2] for call in calls))
        self.assertEqual(record['identity'], candidate['identity'])
        self.assertEqual(record['completePlan'], candidate['expectedPlan'])
        self.assertEqual(record['executionSha256'], candidate['expectedExecutionSha256'])

    def test_malformed_v2_registered_table_does_not_fall_back_to_legacy_history(self):
        from scripts.test_semantic_acceptance import SemanticAcceptanceTest
        fixture = SemanticAcceptanceTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        table_path = fixture.root / 'compatibility/wechat/acceptance-v2.json'
        table_path.write_text('{malformed')
        with patch('scripts.ci.candidate_selection.Path.cwd', return_value=fixture.root), \
             patch('scripts.ci.candidate_selection.prepare_candidate') as prepare, \
             patch('sys.argv', ['candidate_selection', '--app-id', 'wechat', '--mode', 'registered',
                                '--directory', str(fixture.root / 'output')]):
            with self.assertRaisesRegex(ValueError, 'Semantic acceptance table is unreadable'):
                main()
        prepare.assert_not_called()

    def test_registered_migration_fallback_uses_source_linked_pinned_profiles(self):
        from scripts.ci.candidate_selection import _registered_expectations
        from scripts.test_semantic_acceptance import SemanticAcceptanceTest
        fixture = SemanticAcceptanceTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)

        expectations = _registered_expectations('wechat', fixture.root)
        self.assertTrue(expectations)
        history = {row['recordId']: row for row in fixture.bundle.historical_records}
        for expected in expectations:
            with self.subTest(record_id=expected['recordId']):
                self.assertEqual(history[expected['recordId']]['source']['url'], expected['source']['url'])
                self.assertEqual(history[expected['recordId']]['source']['apkFiles'], expected['apkFiles'])
                self.assertEqual(expected['identity']['versionCode'],
                                 next(profile['identity']['versionCode'] for profile in fixture.bundle.profiles
                                      if profile['historicalRecordId'] == expected['recordId']))

    def test_registered_recheck_rejects_split_hash_mismatch_and_order_change(self):
        from scripts.ci.candidate_selection import _verify_registered_candidate
        expected = {
            'identity': {'appId': 'wechat', 'versionCode': 1},
            'apkFiles': [{'role': 'base', 'sha256': 'a' * 64},
                         {'role': 'split', 'sha256': 'b' * 64}],
            'completePlan': {'hooks': []}, 'executionSha256': 'c' * 64,
            'source': {},
        }
        candidate = {
            'status': 'PREPARED', 'identity': expected['identity'],
            'apkFiles': [{'role': 'base', 'sha256': 'a' * 64},
                         {'role': 'split', 'sha256': 'b' * 64}],
            'expectedPlan': expected['completePlan'],
            'expectedExecutionSha256': expected['executionSha256'],
        }
        _verify_registered_candidate(candidate, expected)
        for changed_files in (
            [{'role': 'base', 'sha256': 'a' * 64}, {'role': 'split', 'sha256': 'd' * 64}],
            [{'role': 'split', 'sha256': 'b' * 64}, {'role': 'base', 'sha256': 'a' * 64}],
            [{'role': 'base', 'sha256': 'a' * 64}],
        ):
            with self.subTest(files=changed_files), self.assertRaisesRegex(ValueError, 'ordered base/split'):
                _verify_registered_candidate(dict(candidate, apkFiles=changed_files), expected)

    def test_highest_registered_version_collisions_require_exact_base_sha256(self):
        from scripts.ci.candidate_selection import _select_highest_registered
        first = {'identity': {'versionCode': 9, 'apkSha256': 'a' * 64}}
        second = {'identity': {'versionCode': 9, 'apkSha256': 'b' * 64}}
        verified = [{'expected': row} for row in (first, second)]
        with self.assertRaisesRegex(ValueError, 'ambiguous'):
            _select_highest_registered(verified)
        self.assertIs(verified[0], _select_highest_registered(verified, 'a' * 64))
        with self.assertRaisesRegex(ValueError, 'No registered candidate'):
            _select_highest_registered(verified, 'c' * 64)

    def test_explicit_registered_sha_selects_a_matching_lower_version(self):
        from scripts.ci.candidate_selection import _select_highest_registered
        older = {'expected': {'identity': {'versionCode': 8, 'apkSha256': 'a' * 64}}}
        newer = {'expected': {'identity': {'versionCode': 9, 'apkSha256': 'b' * 64}}}
        self.assertIs(older, _select_highest_registered([older, newer], 'a' * 64))

    def test_highest_registered_same_base_with_different_splits_stays_ambiguous(self):
        from scripts.ci.candidate_selection import _select_highest_registered
        digest = 'a' * 64
        verified = [{'expected': {'identity': {'versionCode': 9, 'apkSha256': digest},
                                  'apkFiles': [{'role': 'base', 'sha256': digest},
                                               {'role': 'split', 'sha256': 'b' * 64}]}},
                    {'expected': {'identity': {'versionCode': 9, 'apkSha256': digest},
                                  'apkFiles': [{'role': 'base', 'sha256': digest},
                                               {'role': 'split', 'sha256': 'c' * 64}]}}]
        with self.assertRaisesRegex(ValueError, 'ambiguous'):
            _select_highest_registered(verified, digest)

    def test_registered_prepares_each_record_once_and_copies_verified_artifact(self):
        from scripts.ci.candidate_selection import prepare_registered_candidate
        from scripts.ci.fetch_latest_wechat_apk import file_sha256

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / 'final'
            rows = []
            for version, base_bytes, split_bytes in (
                    (8, b'older base apk', b'older split apk'),
                    (9, b'newer base apk', b'newer split apk')):
                base_sha = hashlib.sha256(base_bytes).hexdigest()
                split_sha = hashlib.sha256(split_bytes).hexdigest()
                rows.append({
                    'recordId': f'record-{version}',
                    'identity': {'appId': 'wechat', 'versionCode': version,
                                 'apkSha256': base_sha, 'packageName': 'com.tencent.mm'},
                    'apkFiles': [{'path': f'apk-inputs/0-{base_sha}.apk', 'role': 'base',
                                  'sha256': base_sha},
                                 {'path': f'apk-inputs/1-{split_sha}.apk', 'role': 'split',
                                  'sha256': split_sha}],
                    'completePlan': {'version': version},
                    'executionSha256': str(version) * 64,
                    'source': {'type': 'official', 'url': f'https://official.example/{version}.apk'},
                    'payloads': (base_bytes, split_bytes),
                })
            calls = []

            def prepare(app_id, url, directory, *, allow_external=True):
                row = next(row for row in rows if row['source']['url'] == url)
                ordinal = sum(call[0] == url for call in calls) + 1
                calls.append((url, ordinal))
                directory = Path(directory)
                apk_files = []
                input_hashes = []
                for index, saved in enumerate(row['apkFiles']):
                    path = directory / saved['path']
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(row['payloads'][index])
                    apk_files.append({key: saved[key] for key in ('path', 'role', 'sha256')})
                    input_hashes.append({'path': saved['path'], 'sha256': saved['sha256']})
                static_path = directory / 'static-check.json'
                static_path.write_text(json.dumps({
                    'appId': app_id, 'identity': row['identity'], 'inputHashes': input_hashes,
                    'prepareOrdinal': ordinal,
                }, sort_keys=True))
                return {
                    'schemaVersion': 2, 'appId': app_id, 'status': 'PREPARED',
                    'identity': row['identity'], 'sourceUrl': url,
                    'apkFiles': apk_files, 'expectedPlan': row['completePlan'],
                    'expectedExecutionSha256': row['executionSha256'],
                    'staticReport': 'static-check.json',
                    'staticReportSha256': file_sha256(static_path),
                }

            with patch('scripts.ci.candidate_selection._registered_expectations',
                       return_value=[{key: row[key] for key in (
                           'recordId', 'identity', 'apkFiles', 'completePlan', 'executionSha256', 'source')}
                                     for row in rows]), \
                 patch('scripts.ci.candidate_selection.prepare_candidate', side_effect=prepare):
                candidate = prepare_registered_candidate(
                    'wechat', output, apk_sha256=rows[0]['identity']['apkSha256'], repository=root)

            self.assertEqual(2, len(calls))
            self.assertEqual([1, 1], [ordinal for _, ordinal in calls])
            self.assertEqual(rows[0]['identity'], candidate['identity'])
            self.assertEqual(rows[0]['completePlan'], candidate['expectedPlan'])
            self.assertEqual(rows[0]['executionSha256'], candidate['expectedExecutionSha256'])
            self.assertEqual(rows[0]['apkFiles'], candidate['apkFiles'])
            for index, saved in enumerate(candidate['apkFiles']):
                copied = output / saved['path']
                self.assertEqual(saved['sha256'], file_sha256(copied))
                self.assertEqual(rows[0]['payloads'][index], copied.read_bytes())
            report_path = output / candidate['staticReport']
            self.assertEqual(candidate['staticReportSha256'], file_sha256(report_path))
            static = json.loads(report_path.read_text())
            self.assertEqual(1, static['prepareOrdinal'])
            self.assertEqual([{'path': row['path'], 'sha256': row['sha256']}
                              for row in candidate['apkFiles']], static['inputHashes'])

    def test_checker_nonzero_returns_fresh_valid_nonselected_semantic_report(self):
        from scripts.ci.candidate_selection import _run_rules_checker
        from scripts.test_semantic_migration import SemanticMigrationTest
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            apk = root / 'candidate.apk'; apk.write_bytes(b'candidate')
            report_path = root / 'static.json'
            report = self._semantic_report('wecom', apk, 'NO_MATCH')

            def run(*args, **kwargs):
                report_path.write_text(json.dumps(report))
                raise __import__('subprocess').CalledProcessError(1, args[0])

            with patch('scripts.ci.candidate_selection.subprocess.run', side_effect=run):
                checked = _run_rules_checker('wecom', apk, report_path, root)
            self.assertEqual('NO_MATCH', checked['selection']['status'])

    def test_checker_does_not_accept_stale_nonselected_report_after_nonzero_exit(self):
        from scripts.ci.candidate_selection import _run_rules_checker
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            apk = root / 'candidate.apk'; apk.write_bytes(b'candidate')
            report_path = root / 'static.json'
            report_path.write_text(json.dumps(self._semantic_report('wecom', apk, 'NO_MATCH')))
            with patch('scripts.ci.candidate_selection.subprocess.run', side_effect=
                       __import__('subprocess').CalledProcessError(1, ['gradlew'])):
                with self.assertRaises(__import__('subprocess').CalledProcessError):
                    _run_rules_checker('wecom', apk, report_path, root)
            self.assertFalse(report_path.exists())

    def test_checker_nonzero_selected_report_is_not_treated_as_a_candidate_result(self):
        from scripts.ci.candidate_selection import _run_rules_checker
        import subprocess
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            apk = root / 'candidate.apk'; apk.write_bytes(b'candidate')
            report_path = root / 'static.json'
            report = self._semantic_report('wecom', apk)

            def run(*args, **kwargs):
                report_path.write_text(json.dumps(report))
                raise subprocess.CalledProcessError(1, args[0])

            with patch('scripts.ci.candidate_selection.subprocess.run', side_effect=run):
                with self.assertRaises(subprocess.CalledProcessError):
                    _run_rules_checker('wecom', apk, report_path, root)

    def test_checker_nonzero_malformed_or_wrong_app_report_is_rejected(self):
        import subprocess
        from scripts.ci.candidate_selection import _run_rules_checker
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            apk = root / 'candidate.apk'; apk.write_bytes(b'candidate')
            report_path = root / 'static.json'
            wrong_app = self._semantic_report('wecom', apk, 'NO_MATCH')
            wrong_app['appId'] = 'qq'
            for payload in (b'{invalid', json.dumps(wrong_app).encode()):
                report_path.unlink(missing_ok=True)

                def run(*args, **kwargs):
                    report_path.write_bytes(payload)
                    raise subprocess.CalledProcessError(1, args[0])

                with patch('scripts.ci.candidate_selection.subprocess.run', side_effect=run):
                    with self.assertRaises(subprocess.CalledProcessError):
                        _run_rules_checker('wecom', apk, report_path, root)

    def test_checker_does_not_swallow_non_subprocess_failures(self):
        from scripts.ci.candidate_selection import _run_rules_checker
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            apk = root / 'candidate.apk'; apk.write_bytes(b'candidate')
            report_path = root / 'static.json'
            with patch('scripts.ci.candidate_selection.subprocess.run', side_effect=RuntimeError('runner failed')):
                with self.assertRaisesRegex(RuntimeError, 'runner failed'):
                    _run_rules_checker('wecom', apk, report_path, root)

    def test_schema2_candidate_binds_full_plan_and_canonical_recognition_identity(self):
        import copy
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            apk = root / 'base.apk'; apk.write_bytes(b'candidate')
            report = self._semantic_report('wecom', apk)
            report['inputHashes'][0]['path'] = str(apk)
            candidate = _candidate_from_static('wecom', 'https://dldir1.qq.com/wework/fixed.apk', apk,
                hashlib.sha256(apk.read_bytes()).hexdigest(), root, root / 'static.json', report)
            expected = {'appId': 'wecom', 'evaluatedRecognizerIds': report['evaluatedRecognizerIds'],
                        'ruleCatalogSha256': report['ruleCatalog']['ruleCatalogSha256']}
            self.assertEqual(2, candidate['schemaVersion'])
            self.assertEqual(report['selection']['plan'], candidate['expectedPlan'])
            self.assertEqual(report['selection']['executionSha256'], candidate['expectedExecutionSha256'])
            self.assertEqual(report['ruleCatalog'], candidate['ruleCatalog'])
            self.assertEqual(report['snapshot'], candidate['snapshot'])
            self.assertIsNot(report['ruleCatalog'], candidate['ruleCatalog'])
            self.assertIsNot(report['snapshot'], candidate['snapshot'])
            self.assertEqual(expected, candidate['recognition'])
            self.assertEqual(hashlib.sha256(json.dumps(expected, sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
                             candidate['recognitionSha256'])
            self.assertNotIn('route', candidate)
            self.assertNotIn('routeSha256', candidate)
            self.assertNotIn('ruleId', candidate['expectedPlan'])
            self.assertEqual(str(apk), report['inputHashes'][0]['path'])

    def test_semantic_report_rejects_legacy_projection_and_invalid_full_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            apk = root / 'base.apk'; apk.write_bytes(b'candidate')
            legacy = {'appId': 'wecom', 'analysisScope': 'STATIC_ONLY', 'identity': self._wecom_identity('a' * 64),
                      'selection': {'status': 'SELECTED'}, 'inputHashes': [], 'routeCandidates': []}
            with self.assertRaises(ValueError):
                _candidate_from_static('wecom', 'https://dldir1.qq.com/wework/fixed.apk', apk,
                    'a' * 64, root, root / 'static.json', legacy)
            for mutation in (
                lambda value: value['selection']['plan']['hooks'][0]['source'].update(dexIndex=999),
                lambda value: value['selection']['plan']['hooks'][0]['configuration'].update(mode='changed'),
                lambda value: value['selection']['plan']['hooks'][0]['shape'].update(returnType='void'),
                lambda value: value.update(recognizers=[]),
            ):
                report = self._semantic_report('wecom', apk)
                mutation(report)
                report['inputHashes'][0]['path'] = str(apk)
                with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                    _candidate_from_static('wecom', 'https://dldir1.qq.com/wework/fixed.apk', apk,
                        hashlib.sha256(apk.read_bytes()).hexdigest(), root, root / 'static.json', report)

    def test_every_nonselected_semantic_status_is_incompatible_with_diagnostics(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            apk = root / 'base.apk'; apk.write_bytes(b'candidate')
            digest = hashlib.sha256(apk.read_bytes()).hexdigest()
            for status in ('NO_MATCH', 'CONFLICT', 'INCOMPLETE', 'EVALUATION_ERROR', 'UNSUPPORTED_INPUT'):
                report = self._semantic_report('wecom', apk, status)
                candidate = _candidate_from_static('wecom', 'https://dldir1.qq.com/wework/fixed.apk', apk,
                    digest, root, root / f'{status}.json', report)
                with self.subTest(status=status):
                    self.assertEqual('INCOMPATIBLE', candidate['status'])
                    self.assertNotIn('expectedPlan', candidate)
                    self.assertEqual(status, candidate['diagnostic']['selection'])

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
                report['snapshot']['archives'].append({
                    'loaderId': report['snapshot']['readyLoaderId'], 'containerIndex': 1,
                    'kind': 'ZIP', 'containerSha256': hashlib.sha256(abi_splits[0].read_bytes()).hexdigest(),
                    'dexSha256': [],
                })
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
        report = CandidateSelectionTest._semantic_report('wecom', apk)
        report['identity'] = identity
        return report

    def _assert_wecom_candidate(self, directory, candidate, identity):
        self.assertEqual(2, candidate['schemaVersion'])
        self.assertEqual('wecom', candidate['appId'])
        self.assertEqual('PREPARED', candidate['status'])
        self.assertEqual(identity, candidate['identity'])
        self.assertEqual(candidate['expectedPlan'], json.loads(
            (directory / candidate['staticReport']).read_text())['selection']['plan'])
        self.assertNotIn('route', candidate)
        static_path = directory / candidate['staticReport']
        static = json.loads(static_path.read_text())
        self.assertEqual(candidate['staticReportSha256'],
                         hashlib.sha256(static_path.read_bytes()).hexdigest())
        self.assertEqual(static['inputHashes'][0]['path'], candidate['apkFiles'][0]['path'])
        for row in candidate['apkFiles']:
            staged = directory / row['path']
            self.assertEqual(row['sha256'], hashlib.sha256(staged.read_bytes()).hexdigest())
        self.assertEqual(candidate['expectedExecutionSha256'], static['selection']['executionSha256'])

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
        candidate_apk = root / 'candidate.apk'
        candidate_apk.write_bytes(b'candidate input')
        static_report = CandidateSelectionTest._semantic_report('qq', candidate_apk)
        execution = static_report['executionIdentity']
        execution.update(fingerprintFileSha256=hashlib.sha256(raw_fingerprint).hexdigest(),
            sourceTreeSha256=fingerprint['sourceTreeSha256'],
            routeConfigurationSha256=fingerprint['routeConfigurationSha256'],
            dependencyLockSha256=fingerprint['dependencyLockSha256'])
        from scripts.ci.semantic_protocol import execution_identity_sha256
        execution['cacheIdentitySha256'] = execution_identity_sha256(execution)
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
            report = self._semantic_report('qq', module)
            with self.assertRaisesRegex(ValueError, 'build-time fingerprint'):
                prepare_module(module, report, Path.cwd())

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
            self.assertEqual(2, prepared['schemaVersion'])
            self.assertEqual('qq', prepared['recognition']['appId'])
            self.assertNotIn('route', prepared)
            self.assertNotIn('routeSha256', prepared)
            with patch('scripts.ci.candidate_selection.subprocess.check_output', return_value='0' * 40 + '\n'):
                verify_module_identity(prepared, repository=root)
            changed = dict(prepared, recognition=dict(prepared['recognition'], appId='wechat'))
            with patch('scripts.ci.candidate_selection.subprocess.check_output', return_value='0' * 40 + '\n'):
                with self.assertRaisesRegex(ValueError, 'recognition or catalog identity'):
                    verify_module_identity(changed, repository=root)

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
