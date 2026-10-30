"""Read and validate immutable, runtime-verified APK acceptance records."""
from datetime import datetime, timezone
from dataclasses import dataclass
import copy
import json
import hashlib
import fcntl
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

from scripts.ci.app_policy import get_policy
from scripts.ci.dex_descriptors import _dex_type_tokens, _java_type_descriptor, _validate_target
from scripts.ci.discover_latest_wechat import validate_official_apk_url
from scripts.ci.download_url import validate_download_url

SCHEMA_VERSION = 1
SEMANTIC_SCHEMA_VERSION = 2
SEMANTIC_ACCEPTANCE_FILENAME = 'acceptance-v2.json'
_DIGEST = re.compile(r"[a-f0-9]{64}")
_CATALOG_CACHE = {}


class RoutingReviewRequired(ValueError):
    """The candidate is not strictly newer than all accepted routing evidence."""

    status = 'ROUTING_REVIEW_REQUIRED'

    def __init__(self):
        super().__init__(self.status)


@dataclass(frozen=True)
class AcceptanceSnapshot:
    path: Path
    raw_bytes: bytes | None
    sha256: str | None
    records_json: bytes
    initial_migration: bool

    @property
    def records(self) -> list[dict]:
        return json.loads(self.records_json)


def read_acceptance_snapshot(path: Path, app_id: str, initial_migration: bool,
                             catalog: dict | None = None,
                             repository: Path | None = None) -> AcceptanceSnapshot:
    path = Path(path).resolve()
    try:
        raw_bytes = path.read_bytes()
    except FileNotFoundError:
        if initial_migration:
            return AcceptanceSnapshot(path, None, None, b'[]', True)
        raise FileNotFoundError(f"Acceptance table is missing: {path}")
    except OSError as error:
        raise ValueError(f"Acceptance table is unreadable: {path}") from error
    records = load_records_bytes(raw_bytes, app_id, initial_migration,
                                 catalog=catalog, repository=repository, source_path=path)
    return AcceptanceSnapshot(path, raw_bytes, hashlib.sha256(raw_bytes).hexdigest(),
                              json.dumps(records, separators=(',', ':')).encode(),
                              bool(initial_migration))


def validate_acceptance_snapshot(snapshot: AcceptanceSnapshot, app_id: str,
                                 initial_migration: bool, catalog: dict | None = None,
                                 repository: Path | None = None) -> list[dict]:
    if (not isinstance(snapshot, AcceptanceSnapshot)
            or snapshot.initial_migration is not bool(initial_migration)):
        raise ValueError('Acceptance snapshot object is invalid for this migration mode')
    if snapshot.raw_bytes is None:
        if not initial_migration or snapshot.sha256 is not None:
            raise ValueError('Only an initial migration may use a missing acceptance snapshot')
        records = []
    else:
        digest = hashlib.sha256(snapshot.raw_bytes).hexdigest()
        if digest != snapshot.sha256:
            raise ValueError('Acceptance snapshot digest does not match its immutable bytes')
        records = load_records_bytes(snapshot.raw_bytes, app_id, initial_migration,
                                     catalog=catalog, repository=repository,
                                     source_path=snapshot.path)
    serialized = json.dumps(records, separators=(',', ':')).encode()
    if serialized != snapshot.records_json:
        raise ValueError('Acceptance snapshot records differ from its validated bytes')
    return records


def _validate_semantic_record(record: dict, app_id: str,
                             repository: Path | None = None) -> tuple[tuple[str, ...], str]:
    from scripts.ci.semantic_protocol import validate_check_report
    from scripts.ci.semantic_validation import _recognition

    required = {'recordId', 'schemaVersion', 'appId', 'status', 'identity', 'apkFiles',
                'completePlan', 'executionSha256', 'recognition', 'recognitionSha256',
                'module', 'source', 'rawStageEvidence', 'staticReport', 'migrationBundle',
                'regressionBaseline', 'evidenceArchive', 'acceptedAt'}
    if not isinstance(record, dict) or set(record) != required:
        raise ValueError('Semantic acceptance record has an invalid schema')
    if (type(record['schemaVersion']) is not int or record['schemaVersion'] != SEMANTIC_SCHEMA_VERSION
            or record['appId'] != app_id or record['status'] != 'VERIFIED'
            or not isinstance(record['recordId'], str) or not record['recordId']
            or not isinstance(record['acceptedAt'], str) or not record['acceptedAt']):
        raise ValueError('Semantic acceptance record identity or status is invalid')

    policy = get_policy(app_id)
    identity = record['identity']
    if (not isinstance(identity, dict) or identity.get('packageName') != policy.package_name
            or identity.get('abi') not in policy.supported_abis
            or identity.get('signerSha256', '').lower() != policy.signer_sha256
            or type(identity.get('versionCode')) is not int or identity['versionCode'] <= 0
            or not _DIGEST.fullmatch(identity.get('apkSha256', ''))):
        raise ValueError('Semantic acceptance APK identity does not match app policy')
    apk_files = record['apkFiles']
    if (not isinstance(apk_files, list) or not apk_files or not isinstance(apk_files[0], dict)
            or apk_files[0].get('role') != 'base'
            or apk_files[0].get('sha256', '').lower() != identity['apkSha256'].lower()):
        raise ValueError('Semantic acceptance APK input identity is incomplete')
    hashes = []
    input_paths = set()
    for index, item in enumerate(apk_files):
        if (not isinstance(item, dict) or set(item) != {'path', 'role', 'sha256'}
                or item.get('role') != ('base' if index == 0 else 'split')
                or not isinstance(item.get('path'), str) or not item['path']
                or Path(item['path']).is_absolute() or '..' in Path(item['path']).parts
                or not _DIGEST.fullmatch(item.get('sha256', ''))):
            raise ValueError('Semantic acceptance APK input identity is malformed')
        path_key = Path(item['path']).as_posix()
        if path_key in input_paths:
            raise ValueError('Semantic acceptance contains a duplicate APK input path')
        input_paths.add(path_key)
        hashes.append(item['sha256'].lower())

    static = record['staticReport']
    validate_check_report(static, expected_app_id=app_id, expected_identity=identity)
    static_inputs = static.get('inputHashes')
    static_paths = [Path(item['path']).as_posix() for item in static_inputs]
    if len(set(static_paths)) != len(static_paths):
        raise ValueError('Semantic static report contains a duplicate APK input path')
    if (len(static_inputs) != len(apk_files)
            or [item['sha256'].lower() for item in static_inputs] != hashes):
        raise ValueError('Semantic acceptance APK input list differs from its static report')
    selection = static['selection']
    if (selection.get('status') != 'SELECTED' or record['completePlan'] != selection.get('plan')
            or record['executionSha256'] != selection.get('executionSha256')):
        raise ValueError('Semantic acceptance CompletePlan differs from its verified static report')
    recognition, recognition_sha = _recognition(static)
    if (record['recognition'] != recognition or record['recognitionSha256'] != recognition_sha
            or not _DIGEST.fullmatch(record['recognitionSha256'])):
        raise ValueError('Semantic acceptance recognition identity differs from its static report')

    module = record['module']
    module_digests = ('sha256', 'sourceTreeSha256', 'ruleCatalogSha256', 'recognitionSha256',
                      'dependencyLockSha256', 'routeConfigurationSha256', 'fingerprintFileSha256')
    if (not isinstance(module, dict) or module.get('schemaVersion') != 2
            or any(not isinstance(module.get(key), str) or not _DIGEST.fullmatch(module[key])
                   for key in module_digests)
            or module['ruleCatalogSha256'] != static['ruleCatalog']['ruleCatalogSha256']
            or module['recognitionSha256'] != recognition_sha
            or module.get('recognition') != recognition
            or module.get('dexkit') != static.get('executionIdentity')):
        raise ValueError('Semantic acceptance module identity is incomplete or inconsistent')
    execution = static['executionIdentity']
    if any(module.get(key) != execution.get(key) for key in
           ('sourceTreeSha256', 'dependencyLockSha256', 'routeConfigurationSha256', 'fingerprintFileSha256')):
        raise ValueError('Semantic acceptance module fingerprint differs from the static execution identity')

    source = record['source']
    if (not isinstance(source, dict) or set(source) not in ({'type', 'url'}, {'type', 'url', 'archiveSha256'})
            or not isinstance(source.get('url'), str)):
        raise ValueError('Semantic acceptance APK source is malformed')
    if source['type'] == 'official':
        validate_official_apk_url(source['url'], policy.official_apk_prefix)
    elif source['type'] == 'signed-download':
        validate_download_url(source['url'])
        if 'archiveSha256' not in source:
            raise ValueError('Signed-download acceptance requires an archive hash')
    else:
        raise ValueError('Semantic acceptance APK source type is invalid')
    if ('archiveSha256' in source and not _DIGEST.fullmatch(source['archiveSha256'])):
        raise ValueError('Semantic acceptance archive hash is invalid')

    evidence = record['rawStageEvidence']
    if (not isinstance(evidence, dict) or set(evidence) != {'static', 'regression', 'runtime', 'files'}):
        raise ValueError('Semantic acceptance raw stage evidence is incomplete')
    for name in ('static', 'regression', 'runtime'):
        reference = evidence[name]
        if (not isinstance(reference, dict) or set(reference) != {'path', 'sha256'}
                or not isinstance(reference['path'], str) or not reference['path']
                or Path(reference['path']).is_absolute() or '..' in Path(reference['path']).parts
                or not _DIGEST.fullmatch(reference['sha256'])):
            raise ValueError(f'Semantic acceptance {name} stage reference is malformed')
    if not isinstance(evidence['files'], list) or not evidence['files']:
        raise ValueError('Semantic acceptance raw runtime evidence is missing')
    for reference in evidence['files']:
        if (not isinstance(reference, dict) or set(reference) != {'path', 'sha256'}
                or not isinstance(reference['path'], str) or not reference['path']
                or Path(reference['path']).is_absolute() or '..' in Path(reference['path']).parts
                or not _DIGEST.fullmatch(reference['sha256'])):
            raise ValueError('Semantic acceptance raw runtime evidence reference is malformed')

    archive = record['evidenceArchive']
    archive_root = f'compatibility/{app_id}/acceptance-evidence/{record["recordId"]}'
    if (not isinstance(archive, dict)
            or set(archive) != {'path', 'manifestPath', 'manifestSha256', 'files'}
            or archive.get('path') != archive_root
            or archive.get('manifestPath') != f'{archive_root}/manifest.json'
            or not isinstance(archive.get('manifestSha256'), str)
            or not _DIGEST.fullmatch(archive['manifestSha256'])
            or not isinstance(archive.get('files'), list)):
        raise ValueError('Semantic acceptance evidence archive binding is malformed')
    archived_paths = set()
    source_paths = set()
    required_archive_refs = [evidence['static'], evidence['regression'], evidence['runtime'], *evidence['files']]
    for item in archive['files']:
        if (not isinstance(item, dict) or set(item) != {'sourcePath', 'path', 'sha256'}
                or not isinstance(item['sourcePath'], str) or not item['sourcePath']
                or not isinstance(item['path'], str) or not item['path'].startswith(archive_root + '/')
                or '..' in Path(item['path']).parts or Path(item['path']).is_absolute()
                or not _DIGEST.fullmatch(item['sha256'])):
            raise ValueError('Semantic acceptance evidence archive file mapping is malformed')
        if item['path'] in archived_paths or item['sourcePath'] in source_paths:
            raise ValueError('Semantic acceptance evidence archive contains duplicate mappings')
        archived_paths.add(item['path'])
        source_paths.add(item['sourcePath'])
    if ([item['path'] for item in archive['files'][:3]]
            != [evidence[name]['path'] for name in ('static', 'regression', 'runtime')]
            or [item['sha256'] for item in archive['files'][:3]]
            != [evidence[name]['sha256'] for name in ('static', 'regression', 'runtime')]
            or [item['path'] for item in archive['files'][3:]]
            != [item['path'] for item in evidence['files']]
            or [item['sha256'] for item in archive['files'][3:]]
            != [item['sha256'] for item in evidence['files']]):
        raise ValueError('Semantic acceptance evidence archive does not bind every stage reference in order')

    bundle = record['migrationBundle']
    if (not isinstance(bundle, dict) or set(bundle) != {'historySha256', 'expectationsSha256', 'recordIds'}
            or any(not isinstance(bundle[key], str) or not _DIGEST.fullmatch(bundle[key])
                   for key in ('historySha256', 'expectationsSha256'))
            or not isinstance(bundle['recordIds'], list) or not bundle['recordIds']
            or any(not isinstance(value, str) or not value for value in bundle['recordIds'])
            or len(set(bundle['recordIds'])) != len(bundle['recordIds'])):
        raise ValueError('Semantic acceptance migration bundle binding is malformed')
    if repository is not None:
        from scripts.ci.semantic_migration import load_migration_bundle
        frozen = load_migration_bundle(repository)
        expected_ids = [profile['historicalRecordId'] for profile in frozen.profiles
                        if profile['appId'] == app_id]
        if (bundle['historySha256'] != frozen.history_sha256
                or bundle['expectationsSha256'] != frozen.expectations_sha256
                or bundle['recordIds'] != expected_ids):
            raise ValueError('Semantic acceptance record differs from the frozen migration bundle')
    baseline = record['regressionBaseline']
    baseline_fields = {'sha256', 'appId', 'migrationHistorySha256',
                       'migrationExpectationsSha256', 'v2Path', 'v2SnapshotSha256',
                       'regressionRecordIds', 'sourceCases'}
    if (not isinstance(baseline, dict) or set(baseline) != baseline_fields
            or baseline.get('appId') != app_id
            or any(not isinstance(baseline.get(key), str) or not _DIGEST.fullmatch(baseline[key])
                   for key in ('sha256', 'migrationHistorySha256', 'migrationExpectationsSha256'))
            or baseline.get('v2Path') != f'compatibility/{app_id}/{SEMANTIC_ACCEPTANCE_FILENAME}'
            or (baseline.get('v2SnapshotSha256') is not None
                and (not isinstance(baseline['v2SnapshotSha256'], str)
                     or not _DIGEST.fullmatch(baseline['v2SnapshotSha256'])))
            or not isinstance(baseline.get('regressionRecordIds'), list)
            or not isinstance(baseline.get('sourceCases'), list)
            or not baseline['sourceCases']):
        raise ValueError('Semantic acceptance regression baseline binding is malformed')
    source_cases = baseline['sourceCases']
    case_ids = []
    migration_case_ids = []
    for case in source_cases:
        if (not isinstance(case, dict)
                or set(case) != {'kind', 'recordId', 'profileId', 'sourceSha256'}
                or case.get('kind') not in {'MIGRATION', 'ACCEPTED_V2'}
                or not isinstance(case.get('recordId'), str) or not case['recordId']
                or (case['kind'] == 'MIGRATION'
                    and (not isinstance(case.get('profileId'), str) or not case['profileId']))
                or (case['kind'] == 'ACCEPTED_V2' and case.get('profileId') is not None)
                or not isinstance(case.get('sourceSha256'), str)
                or not _DIGEST.fullmatch(case['sourceSha256'])):
            raise ValueError('Semantic acceptance regression baseline source case is malformed')
        case_ids.append(case['recordId'])
        if case['kind'] == 'MIGRATION':
            migration_case_ids.append(case['recordId'])
    if (baseline['regressionRecordIds'] != case_ids
            or len(case_ids) != len(set(case_ids))
            or migration_case_ids != bundle['recordIds']
            or baseline['migrationHistorySha256'] != bundle['historySha256']
            or baseline['migrationExpectationsSha256'] != bundle['expectationsSha256']):
        raise ValueError('Semantic acceptance regression baseline differs from its migration or case bindings')
    return (hashes[0], *sorted(hashes[1:])), record['recordId']


def _no_symlink_components(path: Path, boundary: Path | None = None) -> Path:
    path = Path(os.path.abspath(path))
    boundary = Path(os.path.abspath(boundary)) if boundary is not None else Path(path.anchor)
    try:
        relative = path.relative_to(boundary)
    except ValueError as error:
        raise ValueError('Acceptance evidence path escapes its trusted root') from error
    current = boundary
    if current.is_symlink():
        raise ValueError('Acceptance evidence path contains a symbolic link')
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError('Acceptance evidence path contains a symbolic link')
    return path


def _verified_source(path: Path, *, root: Path | None = None) -> Path:
    path = Path(path)
    if not path.is_absolute():
        raise ValueError('Verified acceptance evidence source must be an absolute path')
    path = _no_symlink_components(path, root)
    if not path.is_file():
        raise ValueError(f'Verified acceptance evidence source is missing: {path}')
    return path


def _reference_matches(path: Path, reference: dict, root: Path) -> bool:
    if not isinstance(reference, dict) or not isinstance(reference.get('path'), str):
        return False
    reference_path = Path(reference['path'])
    referenced = reference_path if reference_path.is_absolute() else root / reference_path
    return Path(os.path.abspath(referenced)) == Path(os.path.abspath(path))


def archive_verified_evidence(validation: dict, repository: Path,
                             record_id: str) -> tuple[dict, list[str]]:
    """Copy only verifier-referenced files into a new immutable repository archive."""
    if (not isinstance(validation, dict) or validation.get('outcome') != 'VERIFIED'
            or validation.get('schemaVersion') != 2):
        raise ValueError('Only verified schema2 evidence can be archived')
    repository = Path(repository).resolve()
    inputs = validation.get('inputPaths')
    candidate = validation.get('candidate')
    runtime = validation.get('runtime')
    evidence = validation.get('stageEvidence')
    if not isinstance(inputs, dict) or not isinstance(candidate, dict) \
            or not isinstance(runtime, dict) or not isinstance(evidence, dict):
        raise ValueError('Verified report does not identify its evidence source paths')
    if Path(inputs.get('repository', '')).resolve() != repository:
        raise ValueError('Verified report repository differs from the trusted checkout')

    candidate_report = _verified_source(Path(inputs.get('candidate', '')))
    try:
        if json.loads(candidate_report.read_text()) != candidate:
            raise ValueError('Candidate report changed after runtime verification')
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError('Verified candidate report is unreadable') from error
    candidate_root = candidate_report.parent
    static_relative = Path(candidate.get('staticReport', ''))
    if (not static_relative.parts or static_relative.is_absolute() or '..' in static_relative.parts):
        raise ValueError('Verified candidate static report path is malformed')
    static_source = _verified_source(candidate_root / static_relative, root=candidate_root)
    regression_source = _verified_source(Path(inputs.get('regression', '')))
    evidence_root = Path(inputs.get('evidenceRoot', ''))
    if not evidence_root.is_absolute():
        raise ValueError('Verified runtime evidence root must be absolute')
    evidence_root = _no_symlink_components(evidence_root)
    if not evidence_root.is_dir():
        raise ValueError('Verified runtime evidence root is not a directory')
    runtime_source = _verified_source(Path(inputs.get('smoke', '')), root=evidence_root)
    runtime_ref = runtime.get('evidenceReport')
    if not isinstance(runtime_ref, dict) or not isinstance(runtime_ref.get('path'), str):
        raise ValueError('Verified runtime report reference is missing')
    runtime_from_root = _verified_source(evidence_root / runtime_ref['path'], root=evidence_root)
    if runtime_from_root != runtime_source:
        raise ValueError('Verified runtime report path differs from its evidence-root input')
    if (not _reference_matches(static_source, evidence.get('static'), candidate_root)
            or not _reference_matches(regression_source, evidence.get('regression'), repository)
            or not _reference_matches(runtime_source, evidence.get('runtime'), candidate_root)):
        raise ValueError('Verified stage evidence paths differ from the exact verifier inputs')

    sources = [
        ('static', static_source, evidence.get('static')),
        ('regression', regression_source, evidence.get('regression')),
        ('runtime', runtime_source, evidence.get('runtime')),
    ]
    raw_files = runtime.get('evidence')
    if not isinstance(raw_files, list) or not raw_files:
        raise ValueError('Verified raw runtime evidence list is missing')
    for index, reference in enumerate(raw_files):
        if not isinstance(reference, dict) or not isinstance(reference.get('path'), str):
            raise ValueError('Verified raw runtime evidence reference is malformed')
        source = _verified_source(evidence_root / reference['path'], root=evidence_root)
        sources.append((f'raw-{index:04d}', source, reference))

    checked = []
    for label, source, reference in sources:
        if (not isinstance(reference, dict) or set(reference) != {'path', 'sha256'}
                or not re.fullmatch(r'[a-fA-F0-9]{64}',
                str(reference.get('sha256', '')))
                or hashlib.sha256(source.read_bytes()).hexdigest() != reference['sha256'].lower()):
            raise ValueError(f'Verified {label} evidence source hash changed')
        checked.append((label, source, reference['sha256'].lower()))

    if not isinstance(record_id, str) or not re.fullmatch(r'[0-9a-fA-F-]{36}', record_id):
        raise ValueError('Acceptance archive recordId is malformed')
    archive_parent = repository / 'compatibility' / candidate['appId'] / 'acceptance-evidence'
    app_dir = repository / 'compatibility' / candidate['appId']
    try:
        app_dir.resolve().relative_to(repository)
    except ValueError as error:
        raise ValueError('Acceptance evidence directory escapes the trusted checkout') from error
    _no_symlink_components(app_dir, repository)
    parent_created = False
    if not archive_parent.exists():
        archive_parent.mkdir()
        parent_created = True
    _no_symlink_components(archive_parent, repository)
    archive_dir = archive_parent / record_id
    created = False
    try:
        archive_dir.mkdir()
        created = True
        archive_relative = archive_dir.relative_to(repository).as_posix()
        mappings = []
        for label, source, digest in checked:
            if label == 'static':
                name = 'static-report.json'
            elif label == 'regression':
                name = 'regression-report.json'
            elif label == 'runtime':
                name = 'runtime-report.json'
            else:
                name = f'raw/{label}-{source.name}'
            destination = archive_dir / name
            destination.parent.mkdir(exist_ok=True)
            _no_symlink_components(destination.parent, repository)
            with source.open('rb') as input_file, destination.open('xb') as output_file:
                shutil.copyfileobj(input_file, output_file)
                output_file.flush()
                os.fsync(output_file.fileno())
            copied_sha = hashlib.sha256(destination.read_bytes()).hexdigest()
            if copied_sha != digest or hashlib.sha256(source.read_bytes()).hexdigest() != digest:
                raise ValueError(f'Acceptance evidence changed while archiving: {source}')
            mappings.append({'sourcePath': str(source),
                             'path': f'{archive_relative}/{name}', 'sha256': digest})
        manifest_path = f'{archive_relative}/manifest.json'
        manifest = {'schemaVersion': 1, 'recordId': record_id, 'files': mappings}
        manifest_file = archive_dir / 'manifest.json'
        with manifest_file.open('x', encoding='utf-8') as output:
            output.write(json.dumps(manifest, indent=2) + '\n')
            output.flush()
            os.fsync(output.fileno())
        manifest_sha = hashlib.sha256(manifest_file.read_bytes()).hexdigest()
        evidence_archive = {'path': archive_relative, 'manifestPath': manifest_path,
                            'manifestSha256': manifest_sha,
                            'files': mappings}
        evidence_paths = [item['path'] for item in mappings] + [manifest_path]
        return evidence_archive, evidence_paths
    except BaseException:
        if created:
            shutil.rmtree(archive_dir, ignore_errors=True)
        if parent_created:
            try:
                archive_parent.rmdir()
            except OSError:
                pass
        raise


def build_semantic_acceptance_record(validation: dict, *, evidence_archive: dict | None = None,
                                     record_id: str | None = None) -> dict:
    """Build a schema2 acceptance draft only from a fully verified report."""
    if (not isinstance(validation, dict) or validation.get('schemaVersion') != 2
            or validation.get('outcome') != 'VERIFIED'):
        raise ValueError('Only a schema2 VERIFIED report can produce a semantic acceptance draft')
    candidate = validation.get('candidate')
    module = validation.get('module')
    static = validation.get('static')
    runtime = validation.get('runtime')
    bundle = validation.get('migrationBundle')
    stage_evidence = validation.get('stageEvidence')
    evidence_archive = evidence_archive or validation.get('evidenceArchive')
    if not isinstance(evidence_archive, dict):
        raise ValueError('Verified acceptance evidence must be archived before registration')
    archived = evidence_archive.get('files')
    if not isinstance(archived, list) or len(archived) < 4:
        raise ValueError('Verified acceptance evidence archive is incomplete')
    if record_id is None:
        record_id = Path(evidence_archive.get('path', '')).name
    if (not isinstance(candidate, dict) or not isinstance(module, dict)
            or not isinstance(static, dict) or not isinstance(runtime, dict)
            or not isinstance(bundle, dict) or not isinstance(stage_evidence, dict)):
        raise ValueError('Verified semantic report is missing acceptance inputs')
    normalized_stage = {name: {'path': archived[index]['path'], 'sha256': archived[index]['sha256']}
                        for index, name in enumerate(('static', 'regression', 'runtime'))}
    normalized_files = [{'path': item['path'], 'sha256': item['sha256']} for item in archived[3:]]
    source_url = candidate.get('sourceUrl')
    try:
        validate_official_apk_url(source_url, get_policy(candidate.get('appId')).official_apk_prefix)
        source_type = 'official'
    except ValueError:
        validate_download_url(source_url)
        source_type = 'signed-download'
    source = {'type': source_type, 'url': source_url}
    archive_sha = candidate.get('sourceArchiveSha256')
    if archive_sha is not None:
        if not isinstance(archive_sha, str) or not _DIGEST.fullmatch(archive_sha):
            raise ValueError('Candidate source archive hash is invalid')
        source['archiveSha256'] = archive_sha
    elif source_type == 'signed-download':
        raise ValueError('Signed-download acceptance requires an archive hash')

    record = {
        'recordId': record_id,
        'schemaVersion': SEMANTIC_SCHEMA_VERSION,
        'appId': candidate.get('appId'),
        'status': 'VERIFIED',
        'identity': copy.deepcopy(candidate.get('identity')),
        'apkFiles': copy.deepcopy(candidate.get('apkFiles')),
        'completePlan': copy.deepcopy(candidate.get('expectedPlan')),
        'executionSha256': candidate.get('expectedExecutionSha256'),
        'recognition': copy.deepcopy(candidate.get('recognition')),
        'recognitionSha256': candidate.get('recognitionSha256'),
        'module': copy.deepcopy(module),
        'source': source,
        'rawStageEvidence': {
            **normalized_stage,
            'files': normalized_files,
        },
        'staticReport': copy.deepcopy(static),
        'migrationBundle': copy.deepcopy(bundle),
        'regressionBaseline': copy.deepcopy(validation.get('regressionBaseline')),
        'evidenceArchive': copy.deepcopy(evidence_archive),
        'acceptedAt': datetime.now(timezone.utc).isoformat(),
    }
    _validate_semantic_record(record, str(record['appId']))
    return record


def validate_semantic_acceptance_table(table: dict, app_id: str,
                                       repository: Path | None = None) -> list[dict]:
    if (not isinstance(table, dict) or set(table) != {'schemaVersion', 'appId', 'records'}
            or type(table.get('schemaVersion')) is not int
            or table['schemaVersion'] != SEMANTIC_SCHEMA_VERSION
            or table.get('appId') != app_id or not isinstance(table.get('records'), list)):
        raise ValueError('Semantic acceptance table schema or appId is invalid')
    seen_ids, seen_inputs = set(), set()
    for record in table['records']:
        input_hashes, record_id = _validate_semantic_record(record, app_id, repository)
        if record_id in seen_ids:
            raise ValueError('Semantic acceptance table contains duplicate recordId values')
        if input_hashes in seen_inputs:
            raise ValueError('Semantic acceptance table contains duplicate APK input')
        seen_ids.add(record_id)
        seen_inputs.add(input_hashes)
    return table['records']


def _semantic_acceptance_path(path: Path) -> Path:
    path = Path(path)
    if path.is_symlink():
        raise ValueError('Semantic acceptance table cannot be a symbolic link')
    return path.parent.resolve() / path.name


def read_semantic_acceptance_table(path: Path, app_id: str,
                                   repository: Path | None = None) -> bytes | None:
    path = _semantic_acceptance_path(path)
    try:
        raw_bytes = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ValueError(f'Semantic acceptance table is unreadable: {path}') from error
    try:
        table = json.loads(raw_bytes.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f'Semantic acceptance table is unreadable: {path}') from error
    validate_semantic_acceptance_table(table, app_id, repository)
    return raw_bytes


def write_semantic_acceptance_table(path: Path, record: dict, app_id: str, *,
                                    expected_existing_bytes: bytes | None,
                                    repository: Path | None = None,
                                    before_replace=None) -> dict:
    """Append one validated semantic record with a locked compare-and-swap write."""
    path = _semantic_acceptance_path(path)
    if _validate_semantic_record(record, app_id, repository)[1] != record.get('recordId'):
        raise ValueError('Semantic acceptance record is invalid')
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + '.lock')
    with lock_path.open('a+b') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        current_bytes = path.read_bytes() if path.exists() else None
        if current_bytes != expected_existing_bytes:
            raise ValueError('Semantic acceptance table changed during registration')
        if current_bytes is None:
            records = []
        else:
            try:
                existing = json.loads(current_bytes.decode('utf-8'))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise ValueError('Existing semantic acceptance table is unreadable') from error
            records = validate_semantic_acceptance_table(existing, app_id, repository)
        table = {'schemaVersion': SEMANTIC_SCHEMA_VERSION, 'appId': app_id,
                 'records': records + [record]}
        validate_semantic_acceptance_table(table, app_id, repository)
        payload = (json.dumps(table, indent=2, ensure_ascii=False) + '\n').encode('utf-8')
        temporary = None
        try:
            fd, temporary = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
            with os.fdopen(fd, 'wb') as output:
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
            if before_replace is not None:
                before_replace()
            os.replace(temporary, path)
            temporary = None
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)
        return table


def cleanup_new_evidence_archive(repository: Path, evidence_archive: dict) -> None:
    """Remove only a UUID archive created by the current registration attempt."""
    repository = Path(repository).resolve()
    relative = evidence_archive.get('path') if isinstance(evidence_archive, dict) else None
    if not isinstance(relative, str) or Path(relative).is_absolute() or '..' in Path(relative).parts:
        return
    archive = repository / relative
    try:
        archive.resolve().relative_to(repository)
        _no_symlink_components(archive, repository)
    except (OSError, ValueError):
        return
    if archive.is_dir():
        shutil.rmtree(archive)


def assert_evidence_archive_unchanged(repository: Path, evidence_archive: dict) -> None:
    repository = Path(repository).resolve()
    relative = evidence_archive.get('path') if isinstance(evidence_archive, dict) else None
    if not isinstance(relative, str) or Path(relative).is_absolute() or '..' in Path(relative).parts:
        raise ValueError('Acceptance evidence archive path is malformed')
    archive = repository / relative
    _no_symlink_components(archive, repository)
    if not archive.is_dir():
        raise ValueError('Acceptance evidence archive directory is missing')
    manifest_path = evidence_archive.get('manifestPath')
    if manifest_path != f'{relative}/manifest.json':
        raise ValueError('Acceptance evidence manifest path is malformed')
    manifest_file = _verified_source(repository / manifest_path, root=repository)
    if hashlib.sha256(manifest_file.read_bytes()).hexdigest() != evidence_archive.get('manifestSha256'):
        raise ValueError('Acceptance evidence manifest hash changed')
    try:
        manifest = json.loads(manifest_file.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError('Acceptance evidence manifest is unreadable') from error
    expected = {'schemaVersion': 1, 'recordId': Path(relative).name,
                'files': evidence_archive.get('files')}
    if manifest != expected or not isinstance(expected['files'], list):
        raise ValueError('Acceptance evidence manifest differs from its record mapping')
    for item in expected['files']:
        if (not isinstance(item, dict) or set(item) != {'sourcePath', 'path', 'sha256'}
                or not item['path'].startswith(relative + '/')):
            raise ValueError('Acceptance evidence archive file reference is malformed')
        archived = _verified_source(repository / item['path'], root=archive)
        if hashlib.sha256(archived.read_bytes()).hexdigest() != item['sha256']:
            raise ValueError('Acceptance evidence archive file hash changed')


def load_rule_catalog(app_id: str, repository: Path | None = None) -> dict:
    repository = Path(repository or Path.cwd()).resolve()
    key = (app_id, str(repository))
    if key in _CATALOG_CACHE:
        return _CATALOG_CACHE[key]
    with tempfile.TemporaryDirectory(prefix='impad-rule-catalog-') as temporary:
        report = Path(temporary) / 'catalog.json'
        arguments = shlex.join(['catalog', '--app-id', app_id, '--report', str(report)])
        subprocess.run(['./gradlew', '--no-daemon', ':compat-checker:run', '--args=' + arguments],
                       cwd=repository, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                       check=True, timeout=300)
        catalog = json.loads(report.read_text())
    if (not isinstance(catalog, dict) or catalog.get('schemaVersion') != 1
            or catalog.get('appId') != app_id
            or not _DIGEST.fullmatch(catalog.get('ruleCatalogSha256', ''))
            or not isinstance(catalog.get('definitions'), list)
            or not isinstance(catalog.get('bindings'), list)
            or not isinstance(catalog.get('strategyIds'), list)):
        raise ValueError('Shared checker returned an invalid rule catalog')
    _CATALOG_CACHE[key] = catalog
    return catalog


def validate_expected_plan(plan: dict, catalog: dict, version_code: int | None = None) -> None:
    if not isinstance(plan, dict) or set(plan) != {'selection', 'ruleId', 'strategyId', 'targets'}:
        raise ValueError('Expected plan has an invalid schema')
    if (plan.get('selection') != 'UNIQUE' or not isinstance(catalog, dict)
            or catalog.get('schemaVersion') != 1
            or not _DIGEST.fullmatch(catalog.get('ruleCatalogSha256', ''))):
        raise ValueError('Expected plan or rule catalog schema is invalid')
    definitions = catalog.get('definitions')
    strategies = catalog.get('strategyIds')
    if (not isinstance(definitions, list) or not isinstance(strategies, list)
            or any(not isinstance(value, str) or not value for value in strategies)
            or len(set(strategies)) != len(strategies)):
        raise ValueError('Expected plan rule catalog is incomplete')
    matching_definitions = [row for row in definitions
                            if isinstance(row, dict) and row.get('ruleId') == plan.get('ruleId')]
    definition = matching_definitions[0] if len(matching_definitions) == 1 else None
    if (definition is None or set(definition) != {'ruleId', 'strategyId', 'requiredTargetIds'}
            or definition.get('strategyId') != plan.get('strategyId')
            or plan.get('strategyId') not in strategies):
        raise ValueError('Expected plan rule or strategy does not match the shared catalog')
    required = definition.get('requiredTargetIds')
    targets = plan.get('targets')
    if (not isinstance(required, list) or not required or len(set(required)) != len(required)
            or not isinstance(targets, dict) or set(targets) != set(required)):
        raise ValueError('Expected plan targets do not match the shared catalog')
    for target_id, target in targets.items():
        _validate_target(target_id, target)
    if version_code is not None:
        bindings = catalog.get('bindings')
        if type(version_code) is not int or version_code <= 0 or not isinstance(bindings, list):
            raise ValueError('Expected plan candidate version or rule bindings are invalid')
        matching_bindings = [row for row in bindings
                             if isinstance(row, dict) and row.get('ruleId') == plan['ruleId']]
        valid_bindings = [row for row in matching_bindings
                          if set(row) == {'ruleId', 'min', 'max'}
                          and type(row.get('min')) is int and row['min'] > 0
                          and (row.get('max') is None
                               or type(row.get('max')) is int and row['max'] >= row['min'])]
        if (len(valid_bindings) != len(matching_bindings)
                or not any(row['min'] <= version_code
                           and (row['max'] is None or version_code <= row['max'])
                           for row in valid_bindings)):
            raise ValueError('Expected plan rule has no valid version binding in the shared catalog')


def derive_record_order(records: list[dict], rule_id: str, record_id: str,
                        apk_sha256: str | None = None) -> dict:
    groups = {}
    seen_orders = {}
    seen_digests = set()
    for record in records:
        saved_rule = record.get('expectedPlan', {}).get('ruleId')
        order = record.get('ruleOrder')
        first_id = record.get('firstRecordId')
        if (not isinstance(saved_rule, str) or type(order) is not int or order < 1
                or not isinstance(first_id, str) or not first_id):
            raise ValueError('Acceptance snapshot has malformed rule order history')
        if saved_rule in seen_orders and seen_orders[saved_rule] != order:
            raise ValueError('Acceptance snapshot has inconsistent rule order history')
        if order in seen_orders.values() and seen_orders.get(saved_rule) != order:
            raise ValueError('Acceptance snapshot reuses a rule order')
        seen_orders[saved_rule] = order
        group = groups.setdefault(saved_rule, [])
        group.append(record)
        digest = record.get('identity', {}).get('apkSha256', '').lower()
        if digest:
            seen_digests.add(digest)
    if apk_sha256 and apk_sha256.lower() in seen_digests:
        raise ValueError('Candidate APK SHA-256 is already present in the validated acceptance snapshot')
    if sorted(seen_orders.values()) != list(range(1, len(seen_orders) + 1)):
        raise ValueError('Acceptance snapshot rule order is not contiguous')
    for saved_rule, group in groups.items():
        first = group[0]
        if (first.get('firstForRule') is not True or first.get('firstRecordId') != first.get('recordId')
                or any(row.get('firstForRule') is not False
                       or row.get('firstRecordId') != first.get('recordId') for row in group[1:])):
            raise ValueError(f'Acceptance snapshot fixed first record is malformed for {saved_rule}')
    if rule_id in groups:
        group = groups[rule_id]
        first = group[0]
        expected_order = seen_orders[rule_id]
        if any(row.get('firstRecordId') != first.get('recordId') for row in group):
            raise ValueError(f'Acceptance snapshot fixed first record is inconsistent for {rule_id}')
        return {'firstForRule': False, 'firstRecordId': first['recordId'], 'ruleOrder': expected_order}
    return {'firstForRule': True, 'firstRecordId': record_id,
            'ruleOrder': max(seen_orders.values(), default=0) + 1}


def propose_forward_binding(bindings: list[dict], records: list[dict],
                            candidate_version: int, new_rule_id: str) -> list[dict]:
    """Propose a forward-only route from validated history without mutating it."""
    if (type(candidate_version) is not int or candidate_version <= 0
            or not isinstance(new_rule_id, str)
            or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_.-]*', new_rule_id)):
        raise ValueError('Candidate version or new rule ID is invalid')
    if not isinstance(bindings, list) or not isinstance(records, list):
        raise ValueError('Bindings and acceptance records must be lists')

    proposed = []
    seen_rules = set()
    for row in bindings:
        if (not isinstance(row, dict) or set(row) != {'ruleId', 'min', 'max'}
                or not isinstance(row.get('ruleId'), str) or not row['ruleId']
                or type(row.get('min')) is not int or row['min'] <= 0
                or (row.get('max') is not None
                    and (type(row['max']) is not int or row['max'] < row['min']))):
            raise ValueError('Existing rule binding is malformed')
        proposed.append(dict(row))
        seen_rules.add(row['ruleId'])
    if new_rule_id in seen_rules:
        raise ValueError(f'New rule ID already exists: {new_rule_id}')

    ordered = sorted(proposed, key=lambda row: (row['min'], row['max'] is None,
                                                 row['max'] if row['max'] is not None else 0))
    for previous, current in zip(ordered, ordered[1:]):
        if previous['max'] is None or previous['max'] >= current['min']:
            raise ValueError('Existing rule bindings overlap')

    accepted_max = {}
    all_accepted = []
    for record in records:
        if not isinstance(record, dict):
            raise ValueError('Acceptance record history is malformed')
        rule_id = record.get('expectedPlan', {}).get('ruleId')
        version = record.get('identity', {}).get('versionCode')
        if (not isinstance(rule_id, str) or type(version) is not int or version <= 0
                or rule_id not in seen_rules):
            raise ValueError('Acceptance record is not bound to an existing rule and version')
        accepted_max[rule_id] = max(accepted_max.get(rule_id, 0), version)
        all_accepted.append(version)
    if all_accepted and candidate_version <= max(all_accepted):
        raise RoutingReviewRequired()
    if not proposed and records:
        raise ValueError('Acceptance history exists without rule bindings')

    for row in proposed:
        if row['max'] is not None and row['max'] < candidate_version:
            continue
        validated_max = accepted_max.get(row['ruleId'])
        if validated_max is None or validated_max < row['min'] or validated_max >= candidate_version:
            raise RoutingReviewRequired()
        row['max'] = validated_max

    proposed.append({'ruleId': new_rule_id, 'min': candidate_version, 'max': None})
    proposed.sort(key=lambda row: (row['min'], row['max'] is None,
                                   row['max'] if row['max'] is not None else 0))
    for previous, current in zip(proposed, proposed[1:]):
        if previous['max'] is None or previous['max'] >= current['min']:
            raise ValueError('Proposed forward bindings overlap')
    return proposed


def load_records(path: Path, app_id: str, initial_migration: bool,
                 catalog: dict | None = None, repository: Path | None = None) -> list[dict]:
    path = Path(path)
    try:
        raw_bytes = path.read_bytes()
    except FileNotFoundError:
        if initial_migration:
            return []
        raise FileNotFoundError(f"Acceptance table is missing: {path}")
    except OSError as error:
        raise ValueError(f"Acceptance table is unreadable: {path}") from error
    return load_records_bytes(raw_bytes, app_id, initial_migration,
                              catalog=catalog, repository=repository, source_path=path)


def load_records_bytes(raw_bytes: bytes, app_id: str, initial_migration: bool,
                       catalog: dict | None = None, repository: Path | None = None,
                       source_path: Path | str = '<bytes>') -> list[dict]:
    if not isinstance(raw_bytes, bytes):
        raise TypeError('Acceptance snapshot must be immutable bytes')
    try:
        document = json.loads(raw_bytes.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"Acceptance table is unreadable: {source_path}") from error
    if (not isinstance(document, dict) or document.get("schemaVersion") != SCHEMA_VERSION
            or document.get("appId") != app_id or not isinstance(document.get("records"), list)):
        raise ValueError("Acceptance table schema or appId is invalid")
    records = document["records"]
    if not records and not initial_migration:
        raise ValueError("Acceptance table is unexpectedly empty")
    policy = get_policy(app_id)
    seen_ids = set()
    first_by_rule = {}
    order_by_rule = {}
    rule_by_order = {}
    for record in records:
        if (not isinstance(record, dict) or record.get("appId") != app_id
                or record.get("status") != "VERIFIED" or not isinstance(record.get("recordId"), str)
                or not record.get("recordId")):
            raise ValueError("Acceptance table contains an invalid or unverified record")
        if record["recordId"] in seen_ids:
            raise ValueError("Acceptance table contains duplicate recordId values")
        seen_ids.add(record["recordId"])
        identity = record.get("identity", {})
        if (not isinstance(identity, dict) or identity.get("packageName") != policy.package_name
                or identity.get("abi") not in policy.supported_abis
                or identity.get("signerSha256", "").lower() != policy.signer_sha256
                or type(identity.get("versionCode")) is not int or identity["versionCode"] <= 0
                or not _DIGEST.fullmatch(identity.get("apkSha256", ""))):
            raise ValueError("Acceptance record APK identity does not match app policy")
        plan = record.get("expectedPlan")
        rule_id = plan.get("ruleId") if isinstance(plan, dict) else None
        if isinstance(plan, dict) and isinstance(plan.get('targets'), dict):
            for target_id, target in plan['targets'].items():
                _validate_target(target_id, target)
        source = record.get("source", {})
        try:
            if not isinstance(source, dict) or source.get('type') not in {'official', 'signed-download'}:
                raise ValueError('Acceptance source type is invalid')
            if source['type'] == 'official':
                validate_official_apk_url(source.get('url', ''), policy.official_apk_prefix)
            else:
                validate_download_url(source.get('url', ''))
            if source.get('archiveSha256') is not None and not _DIGEST.fullmatch(str(source['archiveSha256'])):
                raise ValueError('Acceptance source archive hash is invalid')
            if source['type'] == 'signed-download' and not source.get('archiveSha256'):
                raise ValueError('Signed download acceptance requires a pinned archive hash')
        except ValueError as error:
            raise ValueError("Acceptance record official APK source is invalid") from error
        apk_files = source.get("apkFiles", [])
        if (not isinstance(apk_files, list) or not apk_files or not isinstance(apk_files[0], dict)
                or apk_files[0].get("role") != "base"
                or apk_files[0].get("sha256", "").lower() != identity["apkSha256"].lower()
                or any(not isinstance(item, dict)
                       or item.get("role") != ("base" if index == 0 else "split")
                       or not isinstance(item.get("path"), str) or not item['path']
                       or Path(item['path']).is_absolute() or '..' in Path(item['path']).parts
                       or not isinstance(item.get("sha256"), str)
                       or not _DIGEST.fullmatch(item.get("sha256", ""))
                       for index, item in enumerate(apk_files))):
            raise ValueError("Acceptance record base/split identities are incomplete")
        order = record.get("ruleOrder")
        first_id = record.get('firstRecordId')
        if (not isinstance(rule_id, str) or type(order) is not int or order < 1
                or type(record.get('firstForRule')) is not bool or not isinstance(first_id, str)):
            raise ValueError("Acceptance record is missing its rule identity or order")
        if rule_id in order_by_rule and order_by_rule[rule_id] != order:
            raise ValueError('Acceptance table has inconsistent rule order')
        if order in rule_by_order and rule_by_order[order] != rule_id:
            raise ValueError('Acceptance table reuses a rule order')
        order_by_rule[rule_id] = order
        rule_by_order[order] = rule_id
        if record.get("firstForRule"):
            if rule_id in first_by_rule:
                raise ValueError("Acceptance table has more than one first record for a rule")
            first_by_rule[rule_id] = record
        if not record.get('firstRecordId'):
            raise ValueError('Acceptance table fixed first record ID is missing')
    if records:
        catalog = catalog or load_rule_catalog(app_id, repository)
        if catalog.get('appId') != app_id:
            raise ValueError('Acceptance rule catalog belongs to another application')
        for rule_id, group in ((key, [row for row in records if row['expectedPlan']['ruleId'] == key])
                               for key in order_by_rule):
            if sorted(order_by_rule.values()) != list(range(1, len(order_by_rule) + 1)):
                raise ValueError('Acceptance table rule order is not a contiguous creation sequence')
            first = group[0]
            if (first.get('firstForRule') is not True or first.get('firstRecordId') != first.get('recordId')
                    or first_by_rule.get(rule_id, {}).get('recordId') != first.get('recordId')
                    or any(row.get('firstForRule') is not False
                           or row.get('firstRecordId') != first.get('recordId')
                           for row in group[1:])):
                raise ValueError(f'Acceptance fixed first record is invalid for {rule_id}')
            for record in group:
                route = record.get('route')
                module = record.get('module')
                if (not isinstance(route, dict) or route.get('appId') != app_id
                        or not isinstance(route.get('candidates'), list)
                        or record['expectedPlan']['ruleId'] not in route['candidates']
                        or not isinstance(module, dict)):
                    raise ValueError('Acceptance record route or module identity is incomplete')
                if (not _DIGEST.fullmatch(record.get('ruleCatalogSha256', ''))
                        or record.get('ruleCatalogSha256') != module.get('ruleCatalogSha256')):
                    raise ValueError('Acceptance record catalog digest is not bound to its tested module')
                validate_expected_plan(record['expectedPlan'], catalog,
                                       record['identity']['versionCode'])
                route_payload = {'appId': app_id, 'candidates': route['candidates']}
                route_sha = hashlib.sha256(json.dumps(
                    route_payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
                if (route_sha != module.get('routeSha256')
                        or module.get('route', {}).get('appId') != app_id
                        or module.get('route', {}).get('candidates') != route['candidates']):
                    raise ValueError('Acceptance route fingerprint does not match the tested module')
                module_digests = ('sha256', 'sourceTreeSha256', 'ruleCatalogSha256', 'routeSha256',
                                  'routeConfigurationSha256', 'dependencyLockSha256',
                                  'fingerprintFileSha256')
                if any(not _DIGEST.fullmatch(module.get(key, '')) for key in module_digests):
                    raise ValueError('Acceptance record module source fingerprint is incomplete')
                fingerprint = module.get('fingerprint')
                if (not isinstance(fingerprint, dict) or fingerprint.get('schemaVersion') != 1
                        or fingerprint.get('sourceTreeSha256') != module.get('sourceTreeSha256')
                        or fingerprint.get('routeConfigurationSha256') != module.get('routeConfigurationSha256')
                        or fingerprint.get('dependencyLockSha256') != module.get('dependencyLockSha256')):
                    raise ValueError('Acceptance record APK fingerprint is incomplete or inconsistent')
                stage_evidence = record.get('stageEvidence')
                if (not isinstance(stage_evidence, dict)
                        or set(stage_evidence) != {'static', 'regression', 'runtime'}
                        or any(not isinstance(ref, dict) or not ref.get('path')
                               or not _DIGEST.fullmatch(ref.get('sha256', ''))
                               for ref in stage_evidence.values())):
                    raise ValueError('Acceptance record stage evidence references are incomplete')
    return records


def select_representatives(records: list[dict]) -> list[dict]:
    if not records:
        return []
    app_ids = {record.get("appId") for record in records}
    if len(app_ids) != 1:
        raise ValueError("Representative selection requires one explicit appId")
    rule_orders = {}
    first_by_rule = {}
    groups = {}
    order_to_rule = {}
    for record in records:
        rule_id = record.get("expectedPlan", {}).get("ruleId")
        order = record.get("ruleOrder")
        if not isinstance(rule_id, str) or type(order) is not int or order < 1:
            raise ValueError("Acceptance record is missing its rule identity or order")
        if rule_id in rule_orders and rule_orders[rule_id] != order:
            raise ValueError("A rule has inconsistent creation order")
        if order in order_to_rule and order_to_rule[order] != rule_id:
            raise ValueError('Different rules share an acceptance order')
        rule_orders[rule_id] = order
        order_to_rule[order] = rule_id
        groups.setdefault(rule_id, []).append(record)
        if record.get("firstForRule") is True:
            if rule_id in first_by_rule:
                raise ValueError("A rule has more than one fixed first record")
            first_by_rule[rule_id] = record
        first_id = record.get('firstRecordId')
        if not isinstance(first_id, str) or not first_id:
            raise ValueError('Acceptance record has no fixed first record ID')
    if sorted(rule_orders.values()) != list(range(1, len(rule_orders) + 1)):
        raise ValueError('Acceptance rule order is not a contiguous validated snapshot')
    for rule_id, group in groups.items():
        first = group[0]
        if (first.get('firstForRule') is not True or first.get('firstRecordId') != first.get('recordId')
                or first_by_rule.get(rule_id, {}).get('recordId') != first.get('recordId')
                or any(row.get('firstForRule') is not False
                       or row.get('firstRecordId') != first.get('recordId') for row in group[1:])):
            raise ValueError(f'Fixed first record is invalid for {rule_id}')
    latest_rules = sorted(rule_orders, key=lambda rule: (rule_orders[rule], rule))[-3:]
    selected = []
    seen_sha = set()
    for rule_id in latest_rules:
        representative = first_by_rule.get(rule_id)
        if representative is None:
            raise ValueError(f"The fixed first acceptance record for {rule_id} is missing")
        digest = representative.get("identity", {}).get("apkSha256", "").lower()
        if not _DIGEST.fullmatch(digest):
            raise ValueError(f"The fixed representative for {rule_id} has no valid APK SHA-256")
        if digest not in seen_sha:
            selected.append(representative)
            seen_sha.add(digest)
    return selected


def make_record(candidate: dict, module: dict, validation: dict, *, candidate_path: Path | None = None,
                module_apk: Path | None = None, regression_path: Path | None = None,
                smoke_path: Path | None = None, evidence_root: Path | None = None,
                acceptance_path: Path | None = None, initial_migration: bool = False,
                repository: Path | None = None, conclusion: str = 'success') -> dict:
    if (not isinstance(validation, dict)
            or any(value is None for value in (candidate_path, module_apk, regression_path, smoke_path,
                                               evidence_root, acceptance_path))):
        raise ValueError('Registration requires candidate, module, regression, runtime, and acceptance files')
    from scripts.ci.runtime_task import verify_validation_files
    snapshot_object = validation.get('_acceptanceSnapshotObject')
    if validation.get('acceptanceSnapshot') is not None and snapshot_object is None:
        raise ValueError('Registration requires the verified acceptance snapshot object')
    rebuilt = verify_validation_files(candidate_path, module_apk, regression_path, smoke_path,
                                      evidence_root, conclusion, repository or Path.cwd(),
                                      acceptance_path, initial_migration,
                                      acceptance_snapshot=snapshot_object)
    if (validation.get('stageEvidence') != rebuilt.get('stageEvidence')
            or validation.get('acceptanceSnapshot') != rebuilt.get('acceptanceSnapshot')):
        raise ValueError('Stage evidence or acceptance snapshot changed after unified validation')
    if rebuilt.get('outcome') != 'VERIFIED':
        raise ValueError('Unified file-backed validation did not produce VERIFIED')
    candidate = rebuilt['candidate']
    module = rebuilt['module']
    identity = candidate['identity']
    plan = candidate['expectedPlan']
    snapshot_binding = rebuilt.get('acceptanceSnapshot')
    if (snapshot_object is None
            or rebuilt.get('_acceptanceSnapshotObject') is not snapshot_object
            or validation.get('acceptanceSnapshot') != snapshot_binding):
        raise ValueError('Acceptance snapshot changed after unified validation')
    snapshot = validate_acceptance_snapshot(
        snapshot_object, candidate['appId'], initial_migration,
        catalog=rebuilt['static'].get('ruleCatalog'), repository=repository)
    record_id = str(uuid.uuid4())
    order = derive_record_order(snapshot, plan['ruleId'], record_id, identity['apkSha256'])
    runtime = rebuilt['runtime']
    evidence = runtime['evidence']
    try:
        validate_official_apk_url(candidate['sourceUrl'], get_policy(candidate['appId']).official_apk_prefix)
        source_type = 'official'
    except ValueError:
        validate_download_url(candidate['sourceUrl'])
        source_type = 'signed-download'
    source = {'type': source_type, 'url': candidate['sourceUrl'], 'apkFiles': candidate['apkFiles']}
    if candidate.get('sourceArchiveSha256'):
        source['archiveSha256'] = candidate['sourceArchiveSha256']
    elif source_type == 'signed-download':
        raise ValueError('Signed download acceptance requires a pinned archive hash')
    record = {
        "recordId": record_id,
        "schemaVersion": SCHEMA_VERSION,
        "appId": candidate["appId"],
        "status": "VERIFIED",
        "identity": identity,
        "source": source,
        "expectedPlan": plan,
        "route": candidate.get("route", {}),
        "module": module,
        "ruleCatalogSha256": module['ruleCatalogSha256'],
        "contract": runtime.get("requestIdentityExpectation", {}).get("contract") or runtime.get("contract"),
        "evidence": evidence,
        "stageEvidence": rebuilt['stageEvidence'],
        "acceptedAt": datetime.now(timezone.utc).isoformat(),
        **order,
        "supersedes": candidate.get("supersedes"),
    }
    return record
