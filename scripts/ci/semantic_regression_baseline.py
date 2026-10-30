"""Combine immutable semantic migration expectations with verified v2 records."""
from dataclasses import dataclass, field
import copy
import hashlib
import json
from pathlib import Path

from scripts.ci.semantic_migration import (SemanticMigrationBundle, compare_fixed_report,
                                            load_migration_bundle)
from scripts.ci.semantic_protocol import validate_check_report


def _json_sha256(value):
    raw = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')
    return hashlib.sha256(raw).hexdigest()


def _case_content_sha256(cases):
    return _json_sha256(list(cases))


def _case_ids(cases):
    return [{'kind': case['kind'], 'recordId': case['recordId'],
             'profileId': case['profileId'], 'appId': case['appId']}
            for case in cases]


def _baseline_sha256(bundle, app_id, v2_sha256, cases):
    v2_source = {'state': 'MISSING'} if v2_sha256 is None else {
        'state': 'PRESENT', 'sha256': v2_sha256,
    }
    return _json_sha256({
        'schemaVersion': 1,
        'appId': app_id,
        'migrationHistorySha256': bundle.history_sha256,
        'migrationExpectationsSha256': bundle.expectations_sha256,
        'v2Source': v2_source,
        'caseIds': _case_ids(cases),
    })


@dataclass(frozen=True)
class SemanticRegressionBaseline:
    migration_bundle: SemanticMigrationBundle
    app_id: str
    cases: tuple[dict, ...]
    v2_path: Path
    v2_raw_bytes: bytes | None
    v2_sha256: str | None
    sha256: str
    _repository: Path = field(repr=False, compare=False)
    _cases_sha256: str = field(repr=False, compare=False)

    def assert_unchanged(self):
        if _case_content_sha256(self.cases) != self._cases_sha256:
            raise ValueError('Semantic regression baseline cases were mutated')
        try:
            current = load_semantic_regression_baseline(self._repository, self.app_id)
        except (OSError, ValueError) as error:
            raise ValueError('Semantic regression baseline changed or became invalid') from error
        if (current.sha256 != self.sha256 or current.v2_path != self.v2_path
                or current.v2_raw_bytes != self.v2_raw_bytes or current.v2_sha256 != self.v2_sha256
                or current.cases != self.cases
                or current.migration_bundle.history_sha256 != self.migration_bundle.history_sha256
                or current.migration_bundle.expectations_sha256 != self.migration_bundle.expectations_sha256):
            raise ValueError('Semantic regression baseline changed during validation')


def _input_identity(case):
    return case['appId'], tuple(row['sha256'].lower() for row in case['apkFiles'])


def _case_snapshot(case, bundle):
    if case['kind'] == 'ACCEPTED_V2':
        return case['savedStaticReport']['snapshot']
    profile = next((row for row in bundle.profiles
                    if row['historicalRecordId'] == case['recordId']), None)
    if profile is None:
        raise ValueError('Migration regression case has no frozen snapshot')
    return profile['snapshot']


def _assert_input_expectations_compatible(cases, bundle):
    by_input = {}
    for case in cases:
        expected = (case['identity'], case['expectedPlan'],
                    case['expectedExecutionSha256'], _case_snapshot(case, bundle))
        input_id = _input_identity(case)
        previous = by_input.setdefault(input_id, expected)
        if previous != expected:
            raise ValueError('Semantic regression cases with the same input have conflicting expectations')


def load_semantic_regression_baseline(repository, app_id, migration_bundle=None):
    """Load fixed migration cases followed by every strictly validated accepted-v2 case."""
    from scripts.ci.app_policy import get_policy
    from scripts.ci.acceptance import read_semantic_acceptance_table, validate_semantic_acceptance_table

    get_policy(app_id)
    repository = Path(repository).resolve()
    current_bundle = load_migration_bundle(repository)
    if migration_bundle is not None:
        if (not isinstance(migration_bundle, SemanticMigrationBundle)
                or migration_bundle.history_sha256 != current_bundle.history_sha256
                or migration_bundle.expectations_sha256 != current_bundle.expectations_sha256
                or migration_bundle.profiles_json != current_bundle.profiles_json
                or migration_bundle.historical_records_json != current_bundle.historical_records_json):
            raise ValueError('Supplied migration bundle differs from pinned repository inputs')
        bundle = migration_bundle
    else:
        bundle = current_bundle

    v2_path = repository / 'compatibility' / app_id / 'acceptance-v2.json'
    v2_raw_bytes = read_semantic_acceptance_table(v2_path, app_id, repository)
    v2_sha256 = hashlib.sha256(v2_raw_bytes).hexdigest() if v2_raw_bytes is not None else None
    if v2_raw_bytes is None:
        v2_records = []
    else:
        try:
            table = json.loads(v2_raw_bytes.decode('utf-8'))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError('Semantic v2 acceptance table is unreadable') from error
        v2_records = validate_semantic_acceptance_table(table, app_id, repository)

    profiles = [profile for profile in bundle.profiles if profile['appId'] == app_id]
    historical = {record['recordId']: record for record in bundle.historical_records
                  if record.get('appId') == app_id}
    cases = []
    for profile in profiles:
        record_id = profile['historicalRecordId']
        record = historical.get(record_id)
        if record is None:
            raise ValueError('Frozen semantic migration source record is missing')
        apk_files = copy.deepcopy(record.get('source', {}).get('apkFiles'))
        if (not isinstance(apk_files, list) or
                [row.get('sha256', '').lower() for row in apk_files]
                != [digest.lower() for digest in profile['apkInputSha256']]):
            raise ValueError('Frozen migration APK input identity differs from its source record')
        cases.append({
            'kind': 'MIGRATION',
            'recordId': record_id,
            'profileId': profile['profileId'],
            'appId': app_id,
            'identity': copy.deepcopy(profile['identity']),
            'apkFiles': apk_files,
            'source': copy.deepcopy(record['source']),
            'expectedPlan': copy.deepcopy(profile['expectedPlan']),
            'expectedExecutionSha256': profile['expectedExecutionSha256'],
            'acceptanceSnapshotSha256': profile['historicalAcceptanceSha256'],
        })

    for record in v2_records:
        static = record['staticReport']
        validate_check_report(static, expected_app_id=app_id, expected_identity=record['identity'])
        if (static['selection']['plan'] != record['completePlan']
                or static['selection']['executionSha256'] != record['executionSha256']
                or [row['sha256'].lower() for row in static['inputHashes']]
                != [row['sha256'].lower() for row in record['apkFiles']]):
            raise ValueError('Accepted-v2 record differs from its saved static expectation')
        cases.append({
            'kind': 'ACCEPTED_V2',
            'recordId': record['recordId'],
            'profileId': None,
            'appId': app_id,
            'identity': copy.deepcopy(record['identity']),
            'apkFiles': copy.deepcopy(record['apkFiles']),
            'source': copy.deepcopy(record['source']),
            'expectedPlan': copy.deepcopy(record['completePlan']),
            'expectedExecutionSha256': record['executionSha256'],
            'acceptanceSnapshotSha256': v2_sha256,
            'savedStaticReport': copy.deepcopy(static),
        })

    record_ids = set()
    for case in cases:
        record_id = case['recordId']
        if record_id in record_ids:
            raise ValueError('Semantic regression baseline contains a conflicting recordId')
        record_ids.add(record_id)
    _assert_input_expectations_compatible(cases, bundle)

    cases_tuple = tuple(cases)
    return SemanticRegressionBaseline(
        migration_bundle=bundle,
        app_id=app_id,
        cases=cases_tuple,
        v2_path=v2_path,
        v2_raw_bytes=v2_raw_bytes,
        v2_sha256=v2_sha256,
        sha256=_baseline_sha256(bundle, app_id, v2_sha256, cases_tuple),
        _repository=repository,
        _cases_sha256=_case_content_sha256(cases_tuple),
    )


def compare_baseline_report(baseline, record_id, report):
    if not isinstance(baseline, SemanticRegressionBaseline):
        raise ValueError('Expected a semantic regression baseline')
    if _case_content_sha256(baseline.cases) != baseline._cases_sha256:
        raise ValueError('Semantic regression baseline cases were mutated')
    matches = [case for case in baseline.cases if case['recordId'] == record_id]
    if len(matches) != 1:
        raise ValueError('No unique case in semantic regression baseline')
    case = matches[0]
    if case['kind'] == 'MIGRATION':
        return compare_fixed_report(baseline.migration_bundle, record_id, report)
    if case['kind'] != 'ACCEPTED_V2':
        raise ValueError('Unknown semantic regression baseline case kind')

    saved = case['savedStaticReport']
    validate_check_report(report, expected_app_id=case['appId'],
                          expected_identity=case['identity'],
                          trusted_catalog=saved['ruleCatalog'])
    if (report['selection']['status'] != 'SELECTED'
            or report['selection']['plan'] != case['expectedPlan']
            or report['selection']['executionSha256'] != case['expectedExecutionSha256']
            or report['snapshot'] != saved['snapshot']
            or [row['sha256'].lower() for row in report['inputHashes']]
            != [row['sha256'].lower() for row in case['apkFiles']]):
        raise ValueError('Accepted-v2 regression differs from its pinned plan, snapshot, execution, or APK inputs')
    return True
