"""Load verified acceptance records and check each selected APK against its saved report."""
from dataclasses import dataclass, field
import copy
import hashlib
import json
from pathlib import Path

from scripts.ci.semantic_protocol import validate_check_report


def _json_sha256(value):
    raw = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')
    return hashlib.sha256(raw).hexdigest()


def _case_content_sha256(cases):
    return _json_sha256(list(cases))


def _assert_input_expectations_compatible(cases):
    by_input = {}
    by_input_features = {}
    for case in cases:
        input_identity = tuple((row['role'], row['sha256'].lower()) for row in case['apkFiles'])
        report = case['savedStaticReport']
        expected = (case['identity'], case['expectedPlan'],
                    case['expectedExecutionSha256'], report['snapshot'])
        previous = by_input.setdefault(input_identity, expected)
        if previous != expected:
            raise ValueError('Semantic regression cases with the same input have conflicting expectations')
        features = report['selection'].get('behaviorFeatures')
        if features and by_input_features.setdefault(input_identity, features) != features:
            raise ValueError('Semantic regression cases with the same input have conflicting behavior evidence')


def _accepted_v2_case(record):
    return {
        'kind': 'ACCEPTED_V2',
        'recordId': record['recordId'],
        'appId': record['appId'],
        'identity': copy.deepcopy(record['identity']),
        'apkFiles': copy.deepcopy(record['apkFiles']),
        'source': copy.deepcopy(record['source']),
        'expectedPlan': copy.deepcopy(record['completePlan']),
        'expectedExecutionSha256': record['executionSha256'],
        'savedStaticReport': copy.deepcopy(record['staticReport']),
        'acceptedAt': record['acceptedAt'],
    }


@dataclass(frozen=True)
class VerifiedRegressionPool:
    app_id: str
    cases: tuple[dict, ...]
    v2_raw_bytes: bytes
    v2_sha256: str
    _repository: Path = field(repr=False)
    _cases_sha256: str = field(repr=False)

    def assert_unchanged(self):
        if _case_content_sha256(self.cases) != self._cases_sha256:
            raise ValueError('Verified regression pool cases were mutated')
        try:
            current = load_verified_regression_pool(self._repository, self.app_id)
        except (OSError, ValueError) as error:
            raise ValueError('Verified regression pool changed or became invalid') from error
        if current.v2_raw_bytes != self.v2_raw_bytes or current.cases != self.cases:
            raise ValueError('Verified regression pool changed during validation')


def load_verified_regression_pool(repository, app_id):
    """Load the v2 corpus and its exact archives without legacy migration inputs."""
    from scripts.ci.app_policy import get_policy
    from scripts.ci.acceptance import assert_evidence_archive_unchanged, read_semantic_acceptance_table

    get_policy(app_id)
    repository = Path(repository).resolve()
    path = repository / 'compatibility' / app_id / 'acceptance-v2.json'
    raw = read_semantic_acceptance_table(path, app_id, repository)
    if raw is None:
        raise ValueError('Verified regression pool requires the v2 acceptance table')
    records = json.loads(raw)['records']
    if not records:
        raise ValueError('Verified regression pool requires at least one accepted sample')
    snapshot = hashlib.sha256(raw).hexdigest()
    cases = []
    for record in records:
        assert_evidence_archive_unchanged(repository, record['evidenceArchive'])
        reference = record['rawStageEvidence']['static']
        stored = json.loads((repository / reference['path']).read_bytes())
        if stored != record['staticReport']:
            raise ValueError('Verified regression static expectation differs from its archive')
        cases.append(_accepted_v2_case(record))
    _assert_input_expectations_compatible(cases)
    cases = tuple(cases)
    return VerifiedRegressionPool(app_id, cases, raw, snapshot, repository, _case_content_sha256(cases))


def compare_baseline_report(baseline, record_id, report):
    if not isinstance(baseline, VerifiedRegressionPool):
        raise ValueError('Expected a verified regression pool')
    if _case_content_sha256(baseline.cases) != baseline._cases_sha256:
        raise ValueError('Semantic regression baseline cases were mutated')
    matches = [case for case in baseline.cases if case['recordId'] == record_id]
    if len(matches) != 1:
        raise ValueError('No unique case in semantic regression baseline')
    case = matches[0]
    saved = case['savedStaticReport']
    validate_check_report(report, expected_app_id=case['appId'],
                          expected_identity=case['identity'],
                          trusted_catalog=saved['ruleCatalog'])
    if (report['selection']['status'] != 'SELECTED'
            or report['selection']['plan'] != case['expectedPlan']
            or report['selection']['executionSha256'] != case['expectedExecutionSha256']
            or report['snapshot'] != saved['snapshot']
            or (saved['selection'].get('behaviorFeatures')
                and report['selection'].get('behaviorFeatures') != saved['selection']['behaviorFeatures'])
            or [row['sha256'].lower() for row in report['inputHashes']]
            != [row['sha256'].lower() for row in case['apkFiles']]):
        raise ValueError('Accepted-v2 regression differs from its pinned plan, snapshot, execution, or APK inputs')
    return True
