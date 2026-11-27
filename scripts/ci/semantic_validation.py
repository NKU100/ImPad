"""Validate static and fixed semantic regression stage evidence."""
import hashlib
import json
from pathlib import Path
import re

from scripts.ci.semantic_regression_baseline import VerifiedRegressionPool
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


def validate_semantic_stage_models(candidate, module, static, regression_report,
                                   repository=Path.cwd(), baseline=None,
                                   grouped_report_path=None):
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

    if (not isinstance(baseline, VerifiedRegressionPool) or not isinstance(regression_report, dict)
            or grouped_report_path is None):
        raise ValueError('Grouped regression requires its verified v2 pool and full report')
    from scripts.ci.grouped_static_regression import (badge_regression_rows,
        regression_baseline_binding, verify_grouped_regression)
    from scripts.ci.static_regression import regression_status
    report = verify_grouped_regression(baseline, grouped_report_path, execution)
    if report != regression_report:
        raise ValueError('Grouped regression report differs from the verified report file')
    if report['appId'] != static['appId']:
        raise ValueError('Grouped regression appId differs from static evidence')
    rows = badge_regression_rows(report)
    status = regression_status(rows)
    if status == 'FAILED':
        raise ValueError('A grouped semantic regression failed')
    baseline.assert_unchanged()
    from scripts.ci.regression_groups import GROUPING_VERSION
    return {'schemaVersion': 2, 'staticStatus': 'PASSED', 'regressionStatus': status,
            'regressionRecordIds': [row['groupId'] for row in report['groups']],
            'v2SnapshotSha256': baseline.v2_sha256,
            'regressionBaseline': regression_baseline_binding(baseline)}
