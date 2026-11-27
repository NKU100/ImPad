import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from scripts.ci.grouped_static_regression import group_report, run_group, run_grouped_regression, validate_group_report, validate_group_results, verify_grouped_regression
from scripts.ci.static_regression import HistoricalApkUnavailable


def group():
    return {'groupId': 'example-group', 'cases': [{'recordId': 'latest'}, {'recordId': 'older'}]}


class GroupedStaticRegressionTest(unittest.TestCase):
    def test_selects_cached_substitute_without_downloading(self):
        fetch = Mock()
        check = Mock(return_value={'path': 'older.json', 'sha256': 'a' * 64})
        selected = group()
        row = run_group(selected, lambda case: Path('older.apk') if case['recordId'] == 'older' else None,
                        fetch, check)
        self.assertEqual('older', row['recordId'])
        self.assertEqual('PASSED', row['status'])
        self.assertEqual([], row['unavailable'])
        fetch.assert_not_called()
        check.assert_called_once_with(selected['cases'][1], Path('older.apk'))
        validate_group_results([selected], [row])

    def test_tries_next_only_when_the_original_is_unavailable(self):
        fetch = Mock(side_effect=[HistoricalApkUnavailable('wrong hash'), Path('older.apk')])
        check = Mock(return_value={'path': 'older.json', 'sha256': 'a' * 64})
        selected = group()
        row = run_group(selected, lambda _: None, fetch, check)
        self.assertEqual([{'recordId': 'latest', 'error': 'wrong hash'}], row['unavailable'])
        self.assertEqual('older', row['recordId'])
        validate_group_results([selected], [row])
        forged = dict(row, unavailable=[])
        with self.assertRaises(ValueError):
            validate_group_results([selected], [forged])

    def test_skips_only_after_all_sources_are_unavailable(self):
        fetch = Mock(side_effect=HistoricalApkUnavailable('HTTP 404'))
        check = Mock()
        selected = group()
        row = run_group(selected, lambda _: None, fetch, check)
        self.assertEqual('SKIPPED', row['status'])
        self.assertEqual(['latest', 'older'], [attempt['recordId'] for attempt in row['unavailable']])
        check.assert_not_called()
        validate_group_results([selected], [row])
        forged = copy.deepcopy(row)
        forged['unavailable'].pop()
        with self.assertRaises(ValueError):
            validate_group_results([selected], [forged])

    def test_a_checked_rule_failure_does_not_try_another_apk(self):
        fetch = Mock(return_value=Path('latest.apk'))
        check = Mock(side_effect=ValueError('semantic difference'))
        with self.assertRaisesRegex(ValueError, 'semantic difference'):
            run_group(group(), lambda _: None, fetch, check)
        fetch.assert_called_once()
        check.assert_called_once()

    def test_verified_pool_does_not_read_legacy_files_and_checks_archives(self):
        from scripts.ci.semantic_regression_baseline import load_verified_regression_pool

        repository = Path(__file__).resolve().parents[1]
        source = repository / 'compatibility/wecom'
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = root / 'compatibility/wecom'
            shutil.copytree(source, app, ignore=shutil.ignore_patterns('acceptance.json'))
            pool = load_verified_regression_pool(root, 'wecom')
            self.assertIn(86692, {case['identity']['versionCode'] for case in pool.cases})
            pool.assert_unchanged()
            record = json.loads(pool.v2_raw_bytes)['records'][0]
            archived = root / record['rawStageEvidence']['runtime']['path']
            archived.write_bytes(archived.read_bytes() + b' ')
            with self.assertRaises(ValueError):
                pool.assert_unchanged()

    def test_actual_sample_report_is_compared_with_its_own_pinned_evidence(self):
        from scripts.ci.semantic_regression_baseline import compare_baseline_report, load_verified_regression_pool

        repository = Path(__file__).resolve().parents[1]
        for app_id in ('wechat', 'qq', 'wecom'):
            with self.subTest(app_id=app_id):
                pool = load_verified_regression_pool(repository, app_id)
                case = pool.cases[0]
                report = copy.deepcopy(case['savedStaticReport'])
                for index, item in enumerate(report['inputHashes']):
                    item['path'] = f'/elsewhere/input-{index}.apk'
                self.assertTrue(compare_baseline_report(pool, case['recordId'], report))

                report['selection']['plan']['hooks'][0]['descriptor'] = 'Ldifferent;->changed()V'
                with self.assertRaises(ValueError):
                    compare_baseline_report(pool, case['recordId'], report)

    def test_current_wecom_pool_falls_back_and_checks_the_actual_apk(self):
        from scripts.ci.semantic_regression_baseline import load_verified_regression_pool

        repository = Path(__file__).resolve().parents[1]
        baseline = load_verified_regression_pool(repository, 'wecom')
        selected = [case for case in baseline.cases if case['kind'] == 'ACCEPTED_V2'
                    and case['identity']['versionCode'] in {84802, 86692}]
        known_apks = {case['identity']['versionCode']: case for case in selected}
        baseline = SimpleNamespace(cases=selected, v2_sha256=baseline.v2_sha256,
                                   v2_raw_bytes=baseline.v2_raw_bytes, app_id='wecom',
                                   assert_unchanged=Mock())
        self.assertIn(86692, known_apks)
        self.assertIn(84802, known_apks)
        with tempfile.TemporaryDirectory() as temporary:
            reports = Path(temporary) / 'reports'
            def checker(_repository, apk, _digest, log, **_kwargs):
                evidence = known_apks[84802]['savedStaticReport']
                log.with_suffix('.json').write_text(json.dumps(evidence))
                self.assertEqual(Path('older.apk'), apk)
                return evidence

            with patch('scripts.ci.grouped_static_regression.cached_historical_apk', return_value=None), \
                 patch('scripts.ci.grouped_static_regression.ensure_apk',
                       side_effect=[HistoricalApkUnavailable('unavailable'), Path('older.apk')]), \
                 patch('scripts.ci.grouped_static_regression.record_apk_inputs', return_value=[Path('older.apk')]), \
                 patch('scripts.ci.grouped_static_regression.check_apk', side_effect=checker), \
                 patch('scripts.ci.grouped_static_regression.compare_baseline_report') as compare:
                report = run_grouped_regression(baseline, reports, Path(temporary) / 'cache', repository)
                received = verify_grouped_regression(baseline, reports / 'results.json',
                                                     known_apks[84802]['savedStaticReport']['executionIdentity'])
                self.assertEqual(report, received)
                path = reports / report['groups'][0]['staticEvidence']['path']
                path.write_bytes(path.read_bytes() + b' ')
                with self.assertRaisesRegex(ValueError, 'static report changed'):
                    verify_grouped_regression(baseline, reports / 'results.json',
                                              known_apks[84802]['savedStaticReport']['executionIdentity'])
        self.assertEqual(1, len(report['groups']))
        result = report['groups'][0]
        self.assertEqual('PASSED', result['status'])
        self.assertEqual(known_apks[86692]['recordId'], result['unavailable'][0]['recordId'])
        self.assertEqual(84802, next(case['identity']['versionCode'] for case in selected
                                     if case['recordId'] == result['recordId']))
        self.assertEqual(2, compare.call_count)
        compare.assert_called_with(baseline, result['recordId'], known_apks[84802]['savedStaticReport'])

    def test_badges_report_group_coverage_for_single_app_and_module_build(self):
        from scripts.ci.build_badge import _static_outcome
        from scripts.ci.compatibility_badge import classify_regression

        selected = group()
        selection = {'version': 1, 'selectedGroups': [selected], 'excludedGroups': []}
        unavailable = run_group(selected, lambda _: None, Mock(side_effect=HistoricalApkUnavailable('HTTP 404')), Mock())
        recovered = run_group(selected, lambda _: None,
                              Mock(side_effect=[HistoricalApkUnavailable('HTTP 404'), Path('older.apk')]),
                              Mock(return_value={'path': 'older.json', 'sha256': 'a' * 64}))
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'results.json'
            for row, expected in ((unavailable, 'PARTIAL'), (recovered, 'PASSED')):
                report = group_report('qq', '2' * 64, selection, [row])
                self.assertEqual(expected, classify_regression(report))
                apps = [dict(report, appId=app) for app in ('wechat', 'qq', 'wecom')]
                path.write_text(json.dumps({'schemaVersion': 1, 'applications': apps}))
                self.assertEqual(expected.lower(), _static_outcome(path))
                path.write_text(json.dumps({'schemaVersion': 1, 'applications': apps[:-1]}))
                self.assertEqual('unknown', _static_outcome(path))

    def test_report_binds_grouping_version_snapshot_and_excluded_groups(self):
        selected = group()
        selection = {'version': 1, 'selectedGroups': [selected],
                     'excludedGroups': [{'groupId': 'excluded'}]}
        row = run_group(selected, lambda _: Path('sample.apk'), Mock(),
                        Mock(return_value={'path': 'latest.json', 'sha256': 'a' * 64}))
        report = group_report('qq', '2' * 64, selection, [row])
        self.assertEqual(1, report['schemaVersion'])
        validate_group_report(report, 'qq', '2' * 64, selection)
        for changed in (dict(report, acceptanceSha256='3' * 64), dict(report, excludedGroupIds=[]),
                        dict(report, groupingVersion=2), dict(report, schemaVersion=True)):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                validate_group_report(changed, 'qq', '2' * 64, selection)

    def test_refuses_to_treat_evidence_for_another_selected_group_as_success(self):
        selected = group()
        row = run_group(selected, lambda _: Path('sample.apk'), Mock(),
                        Mock(return_value={'path': 'latest.json', 'sha256': 'a' * 64}))
        for changed in (dict(row, groupId='other'), dict(row, recordId='outsider'),
                        dict(row, candidateRecordIds=['older', 'latest'])):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                validate_group_results([selected], [changed])
