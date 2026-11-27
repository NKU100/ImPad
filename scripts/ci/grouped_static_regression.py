"""Execute and validate the evidence for a selected historical behavior group."""
import hashlib
import json
from pathlib import Path
import subprocess
from http.client import HTTPException

from scripts.ci.app_policy import POLICIES
from scripts.ci.regression_groups import GROUPING_VERSION, select_regression_groups
from scripts.ci.semantic_regression_baseline import compare_baseline_report
from scripts.ci.static_regression import (HistoricalApkUnavailable, SemanticRegressionMismatch,
    cached_historical_apk, check_apk, classify_regression_failure, ensure_apk, file_sha256,
    record_apk_inputs)


REPORT_SCHEMA_VERSION = 1


def run_group(group, find_cached, fetch, check):
    cases = group['cases']
    candidate_ids = [case['recordId'] for case in cases]
    row = {'groupId': group['groupId'], 'candidateRecordIds': candidate_ids,
           'cacheMissRecordIds': [], 'unavailable': []}

    def evaluate(case, apk, source):
        return {**row, 'status': 'PASSED', 'recordId': case['recordId'],
                'apkSource': source, 'staticEvidence': check(case, apk)}

    for case in cases:
        cached = find_cached(case)
        if cached is not None:
            return evaluate(case, cached, 'cache')
        row['cacheMissRecordIds'].append(case['recordId'])

    for case in cases:
        try:
            apk = fetch(case)
        except HistoricalApkUnavailable as error:
            row['unavailable'].append({'recordId': case['recordId'], 'error': str(error)})
            continue
        return evaluate(case, apk, 'download')

    return {**row, 'status': 'SKIPPED', 'skipReason': 'APK_UNAVAILABLE'}


def validate_group_results(groups, rows):
    if not isinstance(rows, list) or len(rows) != len(groups):
        raise ValueError('Regression must report exactly one result per selected group')
    for group, row in zip(groups, rows):
        _validate_group_result(group, row)


def _validate_group_result(group, row):
    candidate_ids = [case['recordId'] for case in group['cases']]
    if (not isinstance(row, dict) or row.get('groupId') != group['groupId']
            or row.get('candidateRecordIds') != candidate_ids):
        raise ValueError('Regression evidence differs from the selected group and candidate order')
    unavailable = row.get('unavailable')
    if not isinstance(unavailable, list) or any(
            not isinstance(attempt, dict) or set(attempt) != {'recordId', 'error'}
            or not isinstance(attempt['error'], str) or not attempt['error']
            for attempt in unavailable):
        raise ValueError('Regression group download attempts are malformed')
    missed = row.get('cacheMissRecordIds')
    unavailable_ids = [attempt['recordId'] for attempt in unavailable]
    if row.get('status') == 'SKIPPED':
        if (row.get('skipReason') != 'APK_UNAVAILABLE' or missed != candidate_ids
                or unavailable_ids != candidate_ids
                or any(key in row for key in ('recordId', 'staticEvidence', 'apkSource'))):
            raise ValueError('A skipped regression group must exhaust every candidate')
        return
    record_id = row.get('recordId')
    if (row.get('status') != 'PASSED' or record_id not in candidate_ids
            or not isinstance(row.get('staticEvidence'), dict) or 'skipReason' in row):
        raise ValueError('A passed regression group needs a selected sample and static evidence')
    preceding = candidate_ids[:candidate_ids.index(record_id)]
    if row.get('apkSource') == 'cache' and missed == preceding and not unavailable_ids:
        return
    if row.get('apkSource') == 'download' and missed == candidate_ids and unavailable_ids == preceding:
        return
    raise ValueError('Regression group has an invalid cache or download sequence')


def group_report(app_id, acceptance_sha256, selection, rows):
    return {
        'schemaVersion': REPORT_SCHEMA_VERSION,
        'groupingVersion': selection['version'],
        'appId': app_id,
        'acceptanceSha256': acceptance_sha256,
        'excludedGroupIds': [group['groupId'] for group in selection['excludedGroups']],
        'groups': rows,
    }


def validate_group_report(report, app_id, acceptance_sha256, selection):
    if (not isinstance(report, dict) or type(report.get('schemaVersion')) is not int
            or type(report.get('groupingVersion')) is not int
            or report != group_report(app_id, acceptance_sha256, selection, report.get('groups'))):
        raise ValueError('Regression report does not match the acceptance snapshot and grouping policy')
    validate_group_results(selection['selectedGroups'], report['groups'])


def regression_selection(baseline):
    if not baseline.cases or not baseline.v2_sha256:
        raise ValueError('Grouped regression requires a nonempty v2 acceptance snapshot')
    return select_regression_groups(baseline.cases)


def regression_baseline_binding(pool):
    selection = regression_selection(pool)
    return {
        'appId': pool.app_id,
        'v2SnapshotSha256': pool.v2_sha256,
        'groupingVersion': selection['version'],
        'selectedGroups': [
            {'groupId': group['groupId'],
             'candidateRecordIds': [case['recordId'] for case in group['cases']]}
            for group in selection['selectedGroups']
        ],
        'excludedGroupIds': [group['groupId'] for group in selection['excludedGroups']],
    }


def run_grouped_regression(baseline, reports, cache, repository):
    """Prepare one static checker sample per group from the accepted-v2 pool."""
    baseline.assert_unchanged()
    selection = regression_selection(baseline)
    reports.mkdir(parents=True, exist_ok=True)

    def record_for(case):
        return {'identity': case['identity'],
                'source': dict(case['source'], apkFiles=case['apkFiles'])}

    def find_cached(case):
        return cached_historical_apk(record_for(case), cache, baseline.app_id)

    def fetch(case):
        return ensure_apk(record_for(case), cache, baseline.app_id)

    def check(case, apk):
        nonlocal stage
        identity = case['identity']
        inputs = record_apk_inputs(record_for(case), cache, apk)
        log = reports / (hashlib.sha256(case['recordId'].encode()).hexdigest() + '.log')
        stage = 'checker_execution'
        options = {'abi_splits': inputs[1:]} if len(inputs) > 1 else {}
        checked = check_apk(repository, apk, identity['apkSha256'], log,
                            app_id=baseline.app_id, **options)
        stage = 'comparison'
        compare_baseline_report(baseline, case['recordId'], checked)
        static_report = log.with_suffix('.json')
        return {'path': static_report.name, 'sha256': file_sha256(static_report)}

    rows = []
    for group in selection['selectedGroups']:
        stage = 'download'
        try:
            rows.append(run_group(group, find_cached, fetch, check))
        except (OSError, ValueError, HTTPException, subprocess.SubprocessError) as error:
            if isinstance(error, SemanticRegressionMismatch):
                stage = 'checker_report'
            rows.append({'groupId': group['groupId'], 'status': 'FAILED',
                         'candidateRecordIds': [case['recordId'] for case in group['cases']],
                         'failureKind': classify_regression_failure(stage, error), 'error': str(error)})
    baseline.assert_unchanged()
    report = group_report(baseline.app_id, baseline.v2_sha256, selection, rows)
    (reports / 'results.json').write_text(json.dumps(report, indent=2) + '\n')
    return report


def verify_grouped_regression(baseline, report_path, execution_identity):
    """Recompute group selection and verify each actual sample's checker evidence."""
    baseline.assert_unchanged()
    selection = regression_selection(baseline)
    report_path = Path(report_path)
    report = json.loads(report_path.read_bytes())
    validate_group_report(report, baseline.app_id, baseline.v2_sha256, selection)
    root = report_path.parent.resolve()
    for row in report['groups']:
        if row['status'] != 'PASSED':
            continue
        reference = row['staticEvidence']
        name = reference.get('path')
        if (set(reference) != {'path', 'sha256'} or not isinstance(name, str)
                or not name or Path(name).name != name):
            raise ValueError('Invalid grouped static evidence reference')
        evidence = (root / name).resolve()
        if not evidence.is_relative_to(root):
            raise ValueError('Grouped static evidence escapes its report directory')
        raw = evidence.read_bytes()
        if hashlib.sha256(raw).hexdigest() != reference['sha256']:
            raise ValueError('Grouped regression static report changed')
        checked = json.loads(raw)
        compare_baseline_report(baseline, row['recordId'], checked)
        if checked['executionIdentity'] != execution_identity:
            raise ValueError('Grouped regression execution identity differs from current static evidence')
    baseline.assert_unchanged()
    return report


def badge_regression_rows(report):
    """Read grouped evidence for badge classification alongside existing row reports."""
    if not isinstance(report, dict):
        return report
    if 'applications' in report:
        apps = report['applications']
        if (type(report.get('schemaVersion')) is not int or report['schemaVersion'] != REPORT_SCHEMA_VERSION
                or not isinstance(apps, list) or len(apps) != len(POLICIES)):
            return None
        per_app = [_badge_group_rows(app) for app in apps]
        if not all(per_app) or {rows[0]['appId'] for rows in per_app} != set(POLICIES):
            return None
        return [row for rows in per_app for row in rows]
    if 'groupingVersion' in report:
        return _badge_group_rows(report)
    return report


def _badge_group_rows(report):
    if (not isinstance(report, dict) or type(report.get('schemaVersion')) is not int
            or report['schemaVersion'] != REPORT_SCHEMA_VERSION or type(report.get('groupingVersion')) is not int
            or report['groupingVersion'] != GROUPING_VERSION):
        return None
    app = report.get('appId')
    rows = report.get('groups')
    if not isinstance(app, str) or app not in POLICIES or not isinstance(rows, list) or not rows:
        return None

    result = []
    seen = set()
    for row in rows:
        group_id = row.get('groupId') if isinstance(row, dict) else None
        if not isinstance(group_id, str) or not group_id or group_id in seen:
            return None
        seen.add(group_id)
        outcome = {'appId': app, 'recordId': group_id, 'status': row.get('status')}
        if outcome['status'] == 'FAILED':
            result.append(dict(outcome, failureKind=row.get('failureKind')))
            continue
        ids = row.get('candidateRecordIds')
        if (not isinstance(ids, list) or not ids or any(not isinstance(value, str) or not value for value in ids)
                or len(set(ids)) != len(ids)):
            return None
        synthetic = {'groupId': group_id, 'cases': [{'recordId': record_id} for record_id in ids]}
        try:
            _validate_group_result(synthetic, row)
        except ValueError:
            return None
        if outcome['status'] == 'SKIPPED':
            outcome.update(skipReason='APK_UNAVAILABLE', error='All group candidates are unavailable')
        result.append(outcome)
    return result
