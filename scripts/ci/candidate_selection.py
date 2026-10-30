"""Prepare an official APK for latest or registered runtime validation."""
import argparse
import copy
from http.client import HTTPException
import importlib
import json
import re
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path
import hashlib
import zipfile

from scripts.ci.acceptance import SEMANTIC_ACCEPTANCE_FILENAME, read_semantic_acceptance_table
from scripts.ci.app_policy import get_policy
from scripts.ci.apk_inputs import resolve_apk_inputs, unpack_apk_inputs
from scripts.ci.download_url import validate_download_url
from scripts.ci.discover_latest_wechat import discover_from_html, fetch_official_page, validate_official_apk_url
from scripts.ci.fetch_latest_wechat_apk import download_candidate, file_sha256, remote_metadata
from scripts.ci.semantic_protocol import validate_check_report


def _run_rules_checker(app_id: str, apk: Path, report_path: Path,
                       repository: Path | None = None, abi_splits=()) -> dict:
    repository = Path(repository or Path(__file__).resolve().parents[2]).resolve()
    report_path = Path(report_path)
    arguments = ['check-rules', '--app-id', app_id, '--apk', str(Path(apk).resolve()),
                 '--report', str(Path(report_path).resolve())]
    if abi_splits:
        arguments += ['--abi-splits', ','.join(str(Path(path).resolve()) for path in abi_splits)]
    log = Path(report_path).with_suffix('.log')
    try:
        report_path.unlink()
    except FileNotFoundError:
        pass
    with log.open('w') as output:
        try:
            subprocess.run(['./gradlew', '--no-daemon', ':compat-checker:run',
                            '--args=' + shlex.join(arguments)],
                           cwd=repository, stdout=output, stderr=subprocess.STDOUT,
                           check=True, timeout=600)
        except subprocess.CalledProcessError as error:
            try:
                result = json.loads(report_path.read_text())
                validate_check_report(result, expected_app_id=app_id)
            except (OSError, ValueError, TypeError, KeyError, AttributeError):
                raise error from None
            if result['selection']['status'] == 'SELECTED':
                raise error
            return result
    return json.loads(Path(report_path).read_text())


def recognition_identity(static: dict) -> tuple[dict, str]:
    validate_check_report(static)
    catalog_sha = static['ruleCatalog']['ruleCatalogSha256']
    recognition = {
        'appId': static['appId'],
        'evaluatedRecognizerIds': list(static['evaluatedRecognizerIds']),
        'ruleCatalogSha256': catalog_sha,
    }
    digest = hashlib.sha256(json.dumps(
        recognition, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return recognition, digest


def _candidate_from_static(app_id: str, source_url: str, apk: Path, base_digest: str,
                           directory: Path, report_path: Path, static: dict,
                           expected_identity: dict | None = None) -> dict:
    policy = get_policy(app_id)
    validate_check_report(static, expected_app_id=app_id, expected_identity=expected_identity)
    recognition, recognition_sha = recognition_identity(static)
    selection = static.get('selection')
    identity = static.get('identity')
    if (not isinstance(selection, dict)
            or selection.get('status') not in {'SELECTED', 'NO_MATCH', 'CONFLICT', 'INCOMPLETE',
                                               'EVALUATION_ERROR', 'UNSUPPORTED_INPUT'}):
        raise ValueError('Shared checker did not return a recognized rule-selection result')
    selected = selection.get('status') == 'SELECTED'
    if not isinstance(identity, dict):
        raise ValueError('Shared checker did not return an APK identity')
    if identity.get('apkSha256', '').lower() != base_digest.lower():
        raise ValueError('Shared checker identity does not match the exact APK bytes')
    if expected_identity is not None and identity != expected_identity:
        raise ValueError('Shared checker identity differs from the uploaded candidate identity')
    if (identity.get('packageName') != policy.package_name
            or identity.get('abi') not in policy.supported_abis
            or identity.get('signerSha256', '').lower() != policy.signer_sha256
            or type(identity.get('versionCode')) is not int or identity['versionCode'] <= 0):
        raise ValueError('Official candidate package, ABI, version, or signer does not match app policy')
    request_expectation = None
    if app_id == 'qq' and selected:
        from scripts.ci.apps.qq.runtime_probe import manifest_expectation
        request_expectation = manifest_expectation(apk, base_digest)
    input_hashes = copy.deepcopy(static.get('inputHashes'))
    if not isinstance(input_hashes, list) or not input_hashes:
        raise ValueError('Shared checker did not report input APK hashes')
    apk_files = []
    for index, row in enumerate(input_hashes):
        if (not isinstance(row, dict) or not isinstance(row.get('path'), str)
                or not re.fullmatch(r'[a-fA-F0-9]{64}', str(row.get('sha256', '')))):
            raise ValueError('Shared checker returned malformed input APK hashes')
        source = Path(row['path']).resolve()
        digest = row['sha256'].lower()
        if not source.is_file() or file_sha256(source) != digest:
            raise ValueError('Shared checker input no longer matches its reported hash')
        if index == 0 and (source != Path(apk).resolve() or digest != base_digest.lower()):
            raise ValueError('Shared checker base APK input differs from the exact candidate artifact')
        staged = directory / 'apk-inputs' / f'{index}-{digest}.apk'
        staged.parent.mkdir(parents=True, exist_ok=True)
        if source != staged.resolve():
            shutil.copyfile(source, staged)
        if file_sha256(staged) != digest:
            raise ValueError('Staged candidate APK differs from shared checker input')
        relative = staged.relative_to(directory).as_posix()
        apk_files.append({'path': relative, 'role': 'base' if index == 0 else 'split',
                          'sha256': digest})
        input_hashes[index]['path'] = relative
    if apk_files[0]['sha256'] != base_digest.lower():
        raise ValueError('Shared checker did not bind the exact base APK bytes')
    static_copy = copy.deepcopy(static)
    static_copy['inputHashes'] = input_hashes
    Path(report_path).write_text(json.dumps(static_copy, indent=2) + '\n')
    candidate = {
        'schemaVersion': 2,
        'appId': policy.app_id,
        'status': 'PREPARED' if selected else 'INCOMPATIBLE',
        'identity': identity,
        'sourceUrl': source_url,
        'apkFiles': apk_files,
        'ruleCatalog': copy.deepcopy(static['ruleCatalog']),
        'snapshot': copy.deepcopy(static['snapshot']),
        'ruleCatalogSha256': static['ruleCatalog']['ruleCatalogSha256'],
        'recognition': recognition,
        'recognitionSha256': recognition_sha,
        'staticReport': Path(report_path).relative_to(directory).as_posix(),
        'staticReportSha256': file_sha256(report_path),
    }
    if selected:
        candidate['expectedPlan'] = copy.deepcopy(selection['plan'])
        candidate['expectedExecutionSha256'] = selection['executionSha256']
    else:
        candidate['diagnostic'] = {'selection': selection['status'],
                                   'diagnostics': selection['diagnostics'],
                                   'incompleteReasons': selection['incompleteReasons']}
    if app_id == 'qq':
        if request_expectation is not None:
            candidate['requestIdentityExpectation'] = request_expectation
    return candidate


def prepare_candidate(app_id: str, apk_url: str, directory: Path, *, allow_external=True) -> dict:
    policy = get_policy(app_id)
    if allow_external:
        validate_download_url(apk_url)
    else:
        validate_official_apk_url(apk_url, policy.official_apk_prefix)
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    metadata = remote_metadata(apk_url, policy, allow_external=allow_external)
    archive, archive_digest, _, _ = download_candidate(
        apk_url, directory / 'apk-cache', '', metadata, policy, allow_external=allow_external)
    if file_sha256(archive) != archive_digest:
        raise ValueError('Official APK changed after download verification')
    inputs = unpack_apk_inputs(archive, directory / 'unpacked', policy.package_name)
    apk = inputs[0]
    digest = file_sha256(apk)
    report_path = directory / 'static-check.json'
    static = (_run_rules_checker(app_id, apk, report_path, abi_splits=inputs[1:])
              if len(inputs) > 1 else _run_rules_checker(app_id, apk, report_path))
    validate_check_report(static, expected_app_id=app_id)
    if [row.get('sha256') for row in static.get('inputHashes', [])] != [file_sha256(path) for path in inputs]:
        raise ValueError('Shared checker did not bind every downloaded base/split APK')
    candidate = _candidate_from_static(app_id, apk_url, apk, digest.lower(), directory,
                                       report_path, static)
    candidate['sourceArchiveSha256'] = archive_digest
    return candidate


def prepare_discovered_candidate(app_id: str, directory: Path) -> dict:
    policy = get_policy(app_id)
    strategy = importlib.import_module(policy.discovery_strategy)
    discovered = strategy.discover(policy)
    source_url = getattr(discovered, 'url', None)
    validate_official_apk_url(source_url or '', policy.official_apk_prefix)
    return prepare_candidate(app_id, source_url, directory, allow_external=False)


def revalidate_candidate_download(report, cache):
    from scripts.ci.static_regression import ensure_apk, record_apk_inputs
    record = {'identity': report['identity'],
              'source': {'type': 'signed-download', 'url': report['sourceUrl'],
                         'apkFiles': report['apkFiles'],
                         'archiveSha256': report.get('sourceArchiveSha256', report['identity']['apkSha256'])}}
    base = ensure_apk(record, Path(cache), report['appId'])
    return record_apk_inputs(record, Path(cache), base)


def prepare_candidate_from_adaptation_report(app_id: str, adaptation_report: Path,
                                            directory: Path,
                                            repository: Path | None = None) -> dict:
    policy = get_policy(app_id)
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    adaptation_report = Path(adaptation_report).resolve()
    try:
        adaptation_report.relative_to(directory)
    except ValueError as error:
        raise ValueError('Adaptation report must be inside the exact downloaded candidate artifact') from error
    try:
        adaptation = json.loads(adaptation_report.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError('Uploaded adaptation report is unreadable') from error
    identity = adaptation.get('identity') if isinstance(adaptation, dict) else None
    if (not isinstance(adaptation, dict) or adaptation.get('appId') != app_id
            or adaptation.get('status') != 'STATIC_VERIFIED_PENDING_RUNTIME'
            or not isinstance(identity, dict)
            or not re.fullmatch(r'[a-fA-F0-9]{64}', str(identity.get('apkSha256', '')))):
        raise ValueError('Uploaded adaptation report does not match the RULES candidate contract')
    source_url = adaptation.get('sourceUrl')
    validate_download_url(source_url or '')
    digest = identity['apkSha256'].lower()
    apk = directory / f'{app_id}-{digest}.apk'
    if not apk.is_file() or file_sha256(apk) != digest:
        raise ValueError('Exact uploaded candidate APK is missing or differs from its reported identity')
    inputs = resolve_apk_inputs(adaptation, directory) if adaptation.get('apkFiles') else [apk]
    report_path = directory / 'static-check.json'
    static = (_run_rules_checker(app_id, apk, report_path, repository, abi_splits=inputs[1:])
              if len(inputs) > 1 else _run_rules_checker(app_id, apk, report_path, repository))
    validate_check_report(static, expected_app_id=app_id)
    if [row.get('sha256') for row in static.get('inputHashes', [])] != [file_sha256(path) for path in inputs]:
        raise ValueError('Shared checker did not bind every uploaded base/split APK')
    candidate = _candidate_from_static(app_id, source_url, apk, digest, directory,
                                      report_path, static, expected_identity=identity)
    if adaptation.get('sourceArchiveSha256'):
        candidate['sourceArchiveSha256'] = adaptation['sourceArchiveSha256']
    return candidate


_MODULE_SOURCE_TREES = (
    'app/src/main', 'app/src/release/keepRules', 'compat-core/src/main', 'compat-rules/src/main',
    'compat-checker/src/main', 'scripts/ci', '.github/actions/prepare-dexkit-system-deps',
)
_MODULE_SOURCE_FILES = (
    '.github/workflows/runtime-validation.yml',
    'gradle.properties',
    'build.gradle.kts', 'settings.gradle.kts',
    'app/build.gradle.kts', 'compat-core/build.gradle.kts',
    'compat-rules/build.gradle.kts', 'compat-checker/build.gradle.kts',
    'gradle/dexkit-lock.json', 'gradle/libs.versions.toml',
    'gradle/wrapper/gradle-wrapper.properties',
)
_DEPENDENCY_LOCKS = {
    'gradle/dexkit-lock.json', 'gradle/libs.versions.toml',
    'gradle/wrapper/gradle-wrapper.properties',
}


def module_source_hashes(root: Path) -> list[dict]:
    root = Path(root).resolve()
    paths = {root / name for name in _MODULE_SOURCE_FILES if (root / name).is_file()}
    for tree in _MODULE_SOURCE_TREES:
        directory = root / tree
        if directory.is_dir():
            paths.update(path for path in directory.rglob('*') if path.is_file())
    files = []
    for path in sorted(paths, key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        if relative == 'scripts/ci/apps/qq/probe/collector_owner_test.c':
            continue
        if any(part in {'build', '__pycache__', '.gradle'} for part in Path(relative).parts):
            continue
        if path.suffix == '.pyc':
            continue
        files.append({'path': relative, 'sha256': file_sha256(path)})
    return files


def _digest_sources(sources: list[dict]) -> str:
    payload = ''.join(f"{row['path']}\n{row['sha256']}\n" for row in sorted(sources, key=lambda row: row['path']))
    return hashlib.sha256(payload.encode()).hexdigest()


def source_tree_sha256(root: Path) -> str:
    return _digest_sources(module_source_hashes(root))


def route_configuration_sha256(sources: list[dict]) -> str:
    selected = [row for row in sources if row['path'].startswith(
        ('compat-core/src/main/', 'compat-rules/src/main/'))]
    return _digest_sources(selected)


def dependency_lock_sha256(sources: list[dict]) -> str:
    selected = [row for row in sources if row['path'] in _DEPENDENCY_LOCKS]
    if {row['path'] for row in selected} != _DEPENDENCY_LOCKS:
        raise ValueError('A module dependency lock input is missing')
    return _digest_sources(selected)


def read_module_fingerprint(module_apk: Path) -> tuple[dict, str]:
    try:
        with zipfile.ZipFile(module_apk) as archive:
            raw = archive.read('assets/runtime-identity.json')
    except (OSError, KeyError, zipfile.BadZipFile) as error:
        raise ValueError('Module APK has no build-time fingerprint') from error
    try:
        fingerprint = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError('Module APK build-time fingerprint is invalid') from error
    if (not isinstance(fingerprint, dict) or fingerprint.get('schemaVersion') != 1
            or not isinstance(fingerprint.get('moduleSources'), list)):
        raise ValueError('Module APK build-time fingerprint is invalid')
    digest = hashlib.sha256(raw).hexdigest()
    return fingerprint, digest


def prepare_module(module_apk: Path, static_report: dict, repository: Path = Path.cwd()) -> dict:
    repository = Path(repository).resolve()
    module_apk = Path(module_apk).resolve()
    if not module_apk.is_file():
        raise FileNotFoundError(f'Module APK is missing: {module_apk}')
    module_sha = file_sha256(module_apk)
    fingerprint, fingerprint_sha = read_module_fingerprint(module_apk)
    current_sources = module_source_hashes(repository)
    tree_now = _digest_sources(current_sources)
    route_config_now = route_configuration_sha256(current_sources)
    lock_now = dependency_lock_sha256(current_sources)
    if (fingerprint.get('moduleSources') != current_sources
            or fingerprint.get('sourceTreeSha256') != tree_now
            or fingerprint.get('routeConfigurationSha256') != route_config_now
            or fingerprint.get('dependencyLockSha256') != lock_now):
        raise ValueError('Module APK build-time fingerprint does not match current source, routing, or dependency inputs')
    source_commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=repository, text=True).strip()
    execution = static_report.get('executionIdentity', {})
    recognition, recognition_sha = recognition_identity(static_report)
    rule_catalog_sha = recognition['ruleCatalogSha256']
    if (static_report['selection']['status'] != 'SELECTED'
            or execution.get('ruleCatalogSha256') != rule_catalog_sha
            or execution.get('fingerprintFileSha256') != fingerprint_sha
            or execution.get('sourceTreeSha256') != tree_now
            or execution.get('routeConfigurationSha256') != route_config_now
            or execution.get('dependencyLockSha256') != lock_now):
        raise ValueError('Static checker identity does not match the module APK fingerprint')
    module = {
        'schemaVersion': 2,
        'apkPath': str(module_apk), 'sha256': module_sha, 'sourceCommit': source_commit,
        'sourceTreeSha256': tree_now, 'ruleCatalogSha256': rule_catalog_sha,
        'recognition': recognition,
        'recognitionSha256': recognition_sha,
        'routeConfigurationSha256': route_config_now,
        'dependencyLockSha256': lock_now,
        'fingerprintFileSha256': fingerprint_sha,
        'fingerprint': fingerprint,
        'dexkit': execution,
    }
    if file_sha256(module_apk) != module_sha or module_source_hashes(repository) != current_sources:
        raise ValueError('Module or source inputs changed while preparing its identity')
    return module


def verify_module_identity(module: dict, expected_apk: Path | None = None,
                           repository: Path = Path.cwd()) -> None:
    repository = Path(repository).resolve()
    apk = Path(expected_apk or module.get('apkPath', '')).resolve()
    source_apk = Path(module.get('apkPath', '')).resolve()
    if (not apk.is_file() or not source_apk.is_file()
            or file_sha256(apk) != module.get('sha256')
            or file_sha256(source_apk) != module.get('sha256')):
        raise ValueError('Module APK changed after identity preparation')
    fingerprint, fingerprint_sha = read_module_fingerprint(apk)
    sources = module_source_hashes(repository)
    if (fingerprint_sha != module.get('fingerprintFileSha256')
            or fingerprint != module.get('fingerprint')
            or fingerprint.get('moduleSources') != sources
            or fingerprint.get('sourceTreeSha256') != source_tree_sha256(repository)
            or fingerprint.get('dependencyLockSha256') != dependency_lock_sha256(sources)
            or fingerprint.get('routeConfigurationSha256') != route_configuration_sha256(sources)):
        raise ValueError('Module APK build-time source fingerprint changed after identity preparation')
    recognition = module.get('recognition')
    if (module.get('schemaVersion') != 2 or not isinstance(recognition, dict)
            or set(recognition) != {'appId', 'evaluatedRecognizerIds', 'ruleCatalogSha256'}
            or not isinstance(recognition.get('appId'), str)
            or not isinstance(recognition.get('evaluatedRecognizerIds'), list)
            or not all(isinstance(value, str) and value for value in recognition['evaluatedRecognizerIds'])):
        raise ValueError('Module recognition identity is malformed')
    recognition_sha = hashlib.sha256(json.dumps(
        recognition, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    if (recognition_sha != module.get('recognitionSha256')
            or recognition.get('ruleCatalogSha256') != module.get('ruleCatalogSha256')
            or module.get('ruleCatalogSha256') != module.get('dexkit', {}).get('ruleCatalogSha256')):
        raise ValueError('Module recognition or catalog identity changed after preparation')


def write_local_runtime_assets(candidate, app_id, directory):
    policy = get_policy(app_id)
    identity = candidate.get('identity', {}) if isinstance(candidate, dict) else {}
    if identity.get('packageName') != policy.package_name:
        raise ValueError('Candidate package does not match the registered application policy')
    assets_root = Path(directory) / 'assets'
    assets_root.mkdir(parents=True, exist_ok=True)
    scope_path = Path(directory) / 'resources/META-INF/xposed/scope.list'
    scope_path.parent.mkdir(parents=True, exist_ok=True)
    scope_path.write_text(policy.package_name + '\n')


def _registered_expectations(app_id, repository):
    """Load v2 registrations, or exact source-linked frozen records during migration."""
    repository = Path(repository).resolve()
    table_path = repository / 'compatibility' / app_id / SEMANTIC_ACCEPTANCE_FILENAME
    raw = read_semantic_acceptance_table(table_path, app_id, repository)
    if raw is not None:
        table = json.loads(raw.decode('utf-8'))
        return [{
            'recordId': record['recordId'],
            'identity': record['identity'],
            'apkFiles': record['apkFiles'],
            'completePlan': record['completePlan'],
            'executionSha256': record['executionSha256'],
            'source': record['source'],
        } for record in table['records']]

    from scripts.ci.semantic_migration import load_migration_bundle

    bundle = load_migration_bundle(repository)
    profiles = [profile for profile in bundle.profiles if profile['appId'] == app_id]
    historical = {record['recordId']: record for record in bundle.historical_records
                  if record.get('appId') == app_id}
    if not profiles:
        return []
    expectations = []
    for profile in profiles:
        record_id = profile['historicalRecordId']
        record = historical.get(record_id)
        source = record.get('source') if isinstance(record, dict) else None
        if (record is None or record.get('identity') != profile['identity']
                or not isinstance(source, dict) or not isinstance(source.get('apkFiles'), list)):
            raise ValueError('Pinned migration profile has no source-linked historical record')
        source_hashes = [item.get('sha256', '').lower() for item in source['apkFiles']
                         if isinstance(item, dict)]
        if (len(source_hashes) != len(source['apkFiles'])
                or source_hashes != [digest.lower() for digest in profile['apkInputSha256']]):
            raise ValueError('Pinned migration profile APK inputs differ from its historical source')
        expectations.append({
            'recordId': record_id,
            'identity': profile['identity'],
            'apkFiles': source['apkFiles'],
            'completePlan': profile['expectedPlan'],
            'executionSha256': profile['expectedExecutionSha256'],
            'source': {key: value for key, value in source.items()
                       if key in {'type', 'url', 'archiveSha256'}},
        })
    return expectations


def _verify_registered_candidate(candidate, expected):
    if not isinstance(candidate, dict) or candidate.get('status') != 'PREPARED':
        raise ValueError('Registered candidate did not prepare a selected schema2 plan')
    if candidate.get('identity') != expected['identity']:
        raise ValueError('Rechecked APK identity differs from the registered record')

    expected_files = expected['apkFiles']
    actual_files = candidate.get('apkFiles')
    if (not isinstance(actual_files, list) or len(actual_files) != len(expected_files)
            or any(not isinstance(actual, dict)
                   or actual.get('role') != saved.get('role')
                   or str(actual.get('sha256', '')).lower() != str(saved.get('sha256', '')).lower()
                   for actual, saved in zip(actual_files, expected_files))):
        raise ValueError('Rechecked ordered base/split APK hashes differ from the registered record')
    if candidate.get('expectedPlan') != expected['completePlan']:
        raise ValueError('Rechecked CompletePlan differs from the registered record')
    if (str(candidate.get('expectedExecutionSha256', '')).lower()
            != str(expected['executionSha256']).lower()):
        raise ValueError('Rechecked execution SHA-256 differs from the registered record')

    expected_archive = expected['source'].get('archiveSha256')
    if (expected_archive is not None
            and str(candidate.get('sourceArchiveSha256', '')).lower() != expected_archive.lower()):
        raise ValueError('Rechecked source archive SHA-256 differs from the registered record')
    return candidate


def _recheck_registered_candidate(app_id, expected, directory):
    source = expected['source']
    if not isinstance(source, dict) or source.get('type') not in {'official', 'signed-download'}:
        raise ValueError('Registered APK source is malformed')
    candidate = prepare_candidate(
        app_id, source['url'], Path(directory),
        allow_external=source['type'] == 'signed-download')
    return _verify_registered_candidate(candidate, expected)


def _select_highest_registered(verified, apk_sha256=None):
    if not verified:
        raise ValueError('No registered candidate matches the requested APK')
    if apk_sha256:
        verified = [item for item in verified
                      if item['expected']['identity']['apkSha256'].lower() == apk_sha256.lower()]
        if not verified:
            raise ValueError('No registered candidate matches --apk-sha256')
    highest = max(item['expected']['identity']['versionCode'] for item in verified)
    candidates = [item for item in verified if item['expected']['identity']['versionCode'] == highest]
    if len(candidates) != 1:
        raise ValueError('Highest registered APK is ambiguous; specify --apk-sha256')
    return candidates[0]


def _artifact_file(root, relative):
    path = Path(relative)
    if path.is_absolute() or '..' in path.parts or not path.parts:
        raise ValueError('Registered candidate artifact path is not relative to its bundle')
    resolved = (Path(root) / path).resolve()
    try:
        resolved.relative_to(Path(root).resolve())
    except ValueError as error:
        raise ValueError('Registered candidate artifact path escapes its bundle') from error
    return resolved


def _copy_registered_artifact(candidate, source_directory, destination_directory):
    source_directory = Path(source_directory).resolve()
    destination_directory = Path(destination_directory).resolve()
    paths = [row['path'] for row in candidate.get('apkFiles', [])]
    paths.append(candidate.get('staticReport'))
    if not candidate.get('apkFiles') or not candidate.get('staticReport'):
        raise ValueError('Registered candidate artifact is missing APK or static report paths')

    for relative in paths:
        source = _artifact_file(source_directory, relative)
        destination = _artifact_file(destination_directory, relative)
        if not source.is_file():
            raise ValueError('Verified registered candidate artifact is missing')
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)

    copied_hashes = []
    for row in candidate['apkFiles']:
        copied = _artifact_file(destination_directory, row['path'])
        digest = file_sha256(copied)
        if digest != row['sha256'].lower():
            raise ValueError('Copied registered APK differs from its verified ordered input hash')
        copied_hashes.append({'path': row['path'], 'sha256': digest})
    if (not copied_hashes or copied_hashes[0]['sha256']
            != candidate['identity']['apkSha256'].lower()):
        raise ValueError('Copied registered base APK differs from its verified identity')

    source_report = _artifact_file(source_directory, candidate['staticReport'])
    copied_report = _artifact_file(destination_directory, candidate['staticReport'])
    report_sha = candidate['staticReportSha256'].lower()
    if file_sha256(source_report) != report_sha or file_sha256(copied_report) != report_sha:
        raise ValueError('Copied registered static report differs from its verified report hash')
    static = json.loads(copied_report.read_text())
    input_hashes = static.get('inputHashes') if isinstance(static, dict) else None
    if (not isinstance(input_hashes, list) or len(input_hashes) != len(copied_hashes)
            or any(not isinstance(actual, dict)
                   or actual.get('path') != saved['path']
                   or str(actual.get('sha256', '')).lower() != saved['sha256']
                   for actual, saved in zip(input_hashes, copied_hashes))):
        raise ValueError('Copied registered static report does not bind the copied ordered APK inputs')


def prepare_registered_candidate(app_id, directory, apk_sha256=None, repository=None):
    directory = Path(directory).resolve()
    repository = Path(repository or Path.cwd()).resolve()
    expectations = _registered_expectations(app_id, repository)
    if not expectations:
        raise ValueError('No registered semantic candidate is available')

    verified = []
    with tempfile.TemporaryDirectory(prefix='impad-registered-candidates-') as temporary:
        for index, expected in enumerate(expectations):
            artifact_directory = Path(temporary) / str(index)
            candidate = _recheck_registered_candidate(
                app_id, expected, artifact_directory)
            verified.append({'expected': expected, 'candidate': candidate,
                             'directory': artifact_directory})
        selected = _select_highest_registered(verified, apk_sha256)
        _copy_registered_artifact(selected['candidate'], selected['directory'], directory)
        return selected['candidate']


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--app-id', required=True)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--mode', choices=('latest', 'registered'), default='latest')
    parser.add_argument('--apk-sha256', help='Select a registered APK variant by SHA-256')
    parser.add_argument('--apk-url', help='Check an exact HTTPS APK or XAPK with strict app signature validation')
    parser.add_argument('--adaptation-report', type=Path,
                        help='Recheck the exact APK named by an uploaded adaptation report')
    args = parser.parse_args()
    if args.apk_url and args.adaptation_report:
        parser.error('--apk-url and --adaptation-report are mutually exclusive')
    if args.apk_sha256 and not re.fullmatch(r'[a-fA-F0-9]{64}', args.apk_sha256):
        raise ValueError('--apk-sha256 must contain 64 hexadecimal characters')
    policy = get_policy(args.app_id)
    if args.adaptation_report:
        candidate = prepare_candidate_from_adaptation_report(
            args.app_id, args.adaptation_report, args.directory)
        (args.directory / 'candidate-report.json').write_text(json.dumps(candidate, indent=2) + '\n')
        write_local_runtime_assets(candidate, policy.app_id, args.directory)
        return
    if args.apk_url:
        candidate = prepare_candidate(args.app_id, args.apk_url, args.directory)
        base_apk = args.directory / candidate['apkFiles'][0]['path']
        shutil.copy2(base_apk, args.directory / f"{args.app_id}-{candidate['identity']['apkSha256']}.apk")
        (args.directory / 'candidate-report.json').write_text(json.dumps(candidate, indent=2) + '\n')
        write_local_runtime_assets(candidate, policy.app_id, args.directory)
        return
    if args.mode == 'latest':
        candidate = prepare_discovered_candidate(args.app_id, args.directory)
        base_apk = args.directory / candidate['apkFiles'][0]['path']
        shutil.copy2(base_apk, args.directory / f"{args.app_id}-{candidate['identity']['apkSha256']}.apk")
        (args.directory / 'candidate-report.json').write_text(json.dumps(candidate, indent=2) + '\n')
        write_local_runtime_assets(candidate, policy.app_id, args.directory)
        return
    candidate = prepare_registered_candidate(
        args.app_id, args.directory, args.apk_sha256, repository=Path.cwd())
    digest = candidate['identity']['apkSha256']
    base_apk = args.directory / candidate['apkFiles'][0]['path']
    shutil.copy2(base_apk, args.directory / f'{policy.app_id}-{digest}.apk')
    (args.directory / 'candidate-report.json').write_text(json.dumps(candidate, indent=2) + '\n')
    write_local_runtime_assets(candidate, policy.app_id, args.directory)


if __name__ == '__main__':
    main()
