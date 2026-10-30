"""Validate static and fixed semantic regression stage evidence."""
import hashlib
import json
from pathlib import Path
import re

from scripts.ci.semantic_regression_baseline import (SemanticRegressionBaseline,
    compare_baseline_report, load_semantic_regression_baseline)
from scripts.ci.semantic_protocol import validate_check_report

_SHA256 = re.compile(r'[0-9a-fA-F]{64}')


def _digest(value, label):
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError(f'{label} must be a SHA-256 digest')
    return value.lower()


def _recognition(static):
    value = {'appId': static['appId'],
             'evaluatedRecognizerIds': list(static['evaluatedRecognizerIds']),
             'ruleCatalogSha256': static['ruleCatalog']['ruleCatalogSha256']}
    digest = hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return value, digest


def _reference_path(repository, value):
    if not isinstance(value, str) or not value or Path(value).is_absolute() or '..' in Path(value).parts:
        raise ValueError('Static evidence path must be relative and cannot escape the repository')
    path = (repository / value).resolve()
    if not path.is_relative_to(repository):
        raise ValueError('Static evidence path escapes the repository')


def validate_semantic_stage_models(candidate, module, static, regression_rows, bundle,
                                   fixed_reports, repository=Path.cwd(), baseline=None):
    """Check model consistency only; this does not establish runtime acceptance."""
    repository = Path(repository).resolve()
    validate_check_report(static)
    if static['selection']['status'] != 'SELECTED':
        raise ValueError('Static semantic report must select one complete plan')
    expected_catalog = static['ruleCatalog']['ruleCatalogSha256'].lower()
    expected_plan = static['selection']['plan']
    expected_execution = static['selection']['executionSha256'].lower()
    recognition, recognition_sha = _recognition(static)

    if not isinstance(candidate, dict) or type(candidate.get('schemaVersion')) is not int or candidate.get('schemaVersion') != 2:
        raise ValueError('Candidate must use schema version 2')
    if (candidate.get('appId') != static['appId'] or candidate.get('identity') != static['identity']
            or candidate.get('ruleCatalogSha256', '').lower() != expected_catalog
            or candidate.get('ruleCatalog') != static['ruleCatalog']
            or candidate.get('snapshot') != static['snapshot']
            or candidate.get('expectedPlan') != expected_plan
            or candidate.get('expectedExecutionSha256', '').lower() != expected_execution):
        raise ValueError('Candidate identity, catalog, or complete plan differs from static evidence')
    apk_files = candidate.get('apkFiles')
    input_hashes = static['inputHashes']
    if (not isinstance(apk_files, list) or len(apk_files) != len(input_hashes)
            or any(not isinstance(a, dict) or _digest(a.get('sha256'), 'candidate APK hash') != b['sha256'].lower()
                   for a, b in zip(apk_files, input_hashes))):
        raise ValueError('Candidate APK hash list differs from static input hashes')

    if not isinstance(module, dict) or type(module.get('schemaVersion')) is not int or module.get('schemaVersion') != 2:
        raise ValueError('Module must use schema version 2')
    for label, model in (('candidate', candidate), ('module', module)):
        if model.get('recognition') != recognition or model.get('recognitionSha256', '').lower() != recognition_sha:
            raise ValueError(f'{label} recognition identity differs from static evidence')
    execution = static['executionIdentity']
    if (module.get('ruleCatalogSha256', '').lower() != expected_catalog
            or module.get('dexkit') != execution):
        raise ValueError('Module DexKit identity differs from static evidence')
    for key in ('sourceTreeSha256', 'dependencyLockSha256', 'routeConfigurationSha256', 'fingerprintFileSha256'):
        digest = _digest(execution.get(key), f'static executionIdentity.{key}')
        if _digest(module.get(key), f'module.{key}') != digest:
            raise ValueError(f'Module {key} differs from static evidence')

    if 'staticReport' in candidate or 'staticReportSha256' in candidate:
        _reference_path(repository, candidate.get('staticReport'))
        _digest(candidate.get('staticReportSha256'), 'candidate static report hash')

    baseline = baseline or load_semantic_regression_baseline(repository, static['appId'], bundle)
    if not isinstance(baseline, SemanticRegressionBaseline):
        raise ValueError('Expected a semantic regression baseline')
    baseline.assert_unchanged()
    cases = list(baseline.cases)
    expected_ids = [case['recordId'] for case in cases]
    migration_ids = [case['recordId'] for case in cases if case['kind'] == 'MIGRATION']
    if (not cases or not isinstance(regression_rows, list)
            or len(regression_rows) != len(cases) or len({row.get('recordId') for row in regression_rows
                                                           if isinstance(row, dict)}) != len(cases)):
        raise ValueError('Regression rows must contain exactly one row per baseline case')
    if not isinstance(fixed_reports, dict) or set(fixed_reports) != set(expected_ids):
        raise ValueError('Full regression reports must exactly cover every baseline case')
    rows_by_id = {row['recordId']: row for row in regression_rows}
    if set(rows_by_id) != set(expected_ids):
        raise ValueError('Regression rows contain missing or unknown baseline cases')
    expected_source_cases = []
    for case in cases:
        record_id = case['recordId']
        row = rows_by_id[record_id]
        source_case = {'kind': case['kind'], 'recordId': record_id,
                       'profileId': case['profileId'],
                       'sourceSha256': case['acceptanceSnapshotSha256']}
        expected_source_cases.append(source_case)
        if (row.get('status') != 'PASSED' or row.get('appId') != case['appId']
                or row.get('identity') != case['identity'] or row.get('profileId') != case['profileId']
                or row.get('recordId') != record_id or row.get('initialMigration') is not False
                or row.get('acceptanceSnapshotSha256') != case['acceptanceSnapshotSha256']
                or row.get('migrationHistorySha256') != baseline.migration_bundle.history_sha256
                or row.get('migrationExpectationsSha256') != baseline.migration_bundle.expectations_sha256
                or row.get('baselineSha256') != baseline.sha256
                or row.get('v2SnapshotSha256') != baseline.v2_sha256
                or row.get('baselineSourceCase') != source_case):
            raise ValueError('Regression row differs from its pinned baseline source case')
        evidence = row.get('staticEvidence')
        if not isinstance(evidence, dict) or set(evidence) != {'path', 'sha256'}:
            raise ValueError('Regression row must contain a static evidence reference')
        _reference_path(repository, evidence['path'])
        _digest(evidence['sha256'], 'regression static evidence hash')
        report = fixed_reports[record_id]
        compare_baseline_report(baseline, record_id, report)
        if report['executionIdentity'] != execution:
            raise ValueError('Regression execution identity differs from current static evidence')

    baseline.assert_unchanged()
    baseline_summary = {
        'sha256': baseline.sha256,
        'appId': baseline.app_id,
        'migrationHistorySha256': baseline.migration_bundle.history_sha256,
        'migrationExpectationsSha256': baseline.migration_bundle.expectations_sha256,
        'v2Path': baseline.v2_path.relative_to(repository).as_posix(),
        'v2SnapshotSha256': baseline.v2_sha256,
        'regressionRecordIds': expected_ids,
        'sourceCases': expected_source_cases,
    }

    return {'schemaVersion': 2, 'staticStatus': 'PASSED', 'regressionStatus': 'PASSED',
            'migrationHistorySha256': baseline.migration_bundle.history_sha256,
            'migrationExpectationsSha256': baseline.migration_bundle.expectations_sha256,
            'recordIds': migration_ids,
            'regressionRecordIds': expected_ids,
            'baselineSha256': baseline.sha256,
            'v2SnapshotSha256': baseline.v2_sha256,
            'regressionBaseline': baseline_summary}
