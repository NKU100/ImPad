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
from pathlib import Path
import hashlib
import zipfile

from scripts.ci.acceptance import read_acceptance_snapshot, select_representatives
from scripts.ci.app_policy import get_policy
from scripts.ci.discover_latest_wechat import discover_from_html, fetch_official_page, validate_official_apk_url
from scripts.ci.fetch_latest_wechat_apk import download_candidate, file_sha256, remote_metadata


def _run_rules_checker(app_id: str, apk: Path, report_path: Path,
                       repository: Path | None = None) -> dict:
    repository = Path(repository or Path(__file__).resolve().parents[2]).resolve()
    arguments = ['check-rules', '--app-id', app_id, '--apk', str(Path(apk).resolve()),
                 '--report', str(Path(report_path).resolve())]
    log = Path(report_path).with_suffix('.log')
    with log.open('w') as output:
        subprocess.run(['./gradlew', '--no-daemon', ':compat-checker:run',
                        '--args=' + shlex.join(arguments)],
                       cwd=repository, stdout=output, stderr=subprocess.STDOUT,
                       check=True, timeout=600)
    return json.loads(Path(report_path).read_text())


def _candidate_from_static(app_id: str, source_url: str, apk: Path, base_digest: str,
                           directory: Path, report_path: Path, static: dict,
                           expected_identity: dict | None = None) -> dict:
    policy = get_policy(app_id)
    if (not isinstance(static, dict) or static.get('appId') != app_id
            or static.get('analysisScope') != 'STATIC_ONLY'):
        raise ValueError('Shared checker returned an invalid static report')
    selection = static.get('selection')
    identity = static.get('identity')
    if (not isinstance(selection, dict)
            or selection.get('status') not in {'SELECTED', 'NO_MATCH', 'AMBIGUOUS'}):
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
    route_candidates = static.get('routeCandidates')
    if (not isinstance(route_candidates, list) or (selected and not route_candidates)
            or any(not isinstance(value, str) or not value for value in route_candidates)
            or len(set(route_candidates)) != len(route_candidates)):
        raise ValueError('Shared checker did not return a valid route candidate list')
    route = {'appId': app_id, 'candidates': route_candidates}
    input_hashes = static.get('inputHashes')
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
        shutil.copyfile(source, staged)
        if file_sha256(staged) != digest:
            raise ValueError('Staged candidate APK differs from shared checker input')
        row['path'] = staged.relative_to(directory).as_posix()
        apk_files.append({'path': row['path'], 'role': 'base' if index == 0 else 'split',
                          'sha256': digest})
    if apk_files[0]['sha256'] != base_digest.lower():
        raise ValueError('Shared checker did not bind the exact base APK bytes')
    Path(report_path).write_text(json.dumps(static, indent=2) + '\n')
    candidate = {
        'schemaVersion': 1,
        'appId': policy.app_id,
        'status': 'PREPARED' if selected else 'INCOMPATIBLE',
        'identity': identity,
        'sourceUrl': source_url,
        'apkFiles': apk_files,
        'ruleCatalogSha256': static['ruleCatalogSha256'],
        'routeSha256': hashlib.sha256(json.dumps(route, sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
        'staticReport': Path(report_path).relative_to(directory).as_posix(),
        'staticReportSha256': file_sha256(report_path),
    }
    if selected:
        candidate['expectedPlan'] = {'selection': 'UNIQUE', 'ruleId': selection['selectedRuleId'],
                                     'strategyId': selection['strategyId'], 'targets': selection['targets']}
        candidate['route'] = dict(route, samplingMode='DIRECT' if len(route_candidates) == 1 else 'GAP')
    else:
        candidate['diagnostic'] = {'selection': selection['status'],
                                   'routeCandidates': route_candidates,
                                   'blockers': static.get('blockers', [])}
    if app_id == 'qq':
        if request_expectation is not None:
            candidate['requestIdentityExpectation'] = request_expectation
    return candidate


def prepare_candidate(app_id: str, apk_url: str, directory: Path) -> dict:
    policy = get_policy(app_id)
    validate_official_apk_url(apk_url, policy.official_apk_prefix)
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    metadata = remote_metadata(apk_url, policy)
    apk, digest, _, _ = download_candidate(apk_url, directory / 'apk-cache', '', metadata, policy)
    if file_sha256(apk) != digest:
        raise ValueError('Official APK changed after download verification')
    report_path = directory / 'static-check.json'
    static = _run_rules_checker(app_id, apk, report_path)
    return _candidate_from_static(app_id, apk_url, apk, digest.lower(), directory,
                                  report_path, static)


def prepare_discovered_candidate(app_id: str, directory: Path) -> dict:
    policy = get_policy(app_id)
    strategy = importlib.import_module(policy.discovery_strategy)
    discovered = strategy.discover(policy)
    source_url = getattr(discovered, 'url', None)
    validate_official_apk_url(source_url or '', policy.official_apk_prefix)
    return prepare_candidate(app_id, source_url, directory)


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
    validate_official_apk_url(source_url or '', policy.official_apk_prefix)
    digest = identity['apkSha256'].lower()
    apk = directory / f'{app_id}-{digest}.apk'
    if not apk.is_file() or file_sha256(apk) != digest:
        raise ValueError('Exact uploaded candidate APK is missing or differs from its reported identity')
    report_path = directory / 'static-check.json'
    static = _run_rules_checker(app_id, apk, report_path, repository)
    return _candidate_from_static(app_id, source_url, apk, digest, directory,
                                  report_path, static, expected_identity=identity)


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
    rule_catalog_sha = static_report.get('ruleCatalogSha256')
    if (not rule_catalog_sha or execution.get('ruleCatalogSha256') != rule_catalog_sha
            or execution.get('fingerprintFileSha256') != fingerprint_sha
            or execution.get('sourceTreeSha256') != tree_now
            or execution.get('routeConfigurationSha256') != route_config_now
            or execution.get('dependencyLockSha256') != lock_now):
        raise ValueError('Static checker identity does not match the module APK fingerprint')
    route = {'appId': static_report['appId'], 'candidates': static_report['routeCandidates']}
    module = {
        'apkPath': str(module_apk), 'sha256': module_sha, 'sourceCommit': source_commit,
        'sourceTreeSha256': tree_now, 'ruleCatalogSha256': rule_catalog_sha,
        'routeSha256': hashlib.sha256(json.dumps(route, sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
        'routeConfigurationSha256': route_config_now,
        'dependencyLockSha256': lock_now,
        'fingerprintFileSha256': fingerprint_sha,
        'fingerprint': fingerprint,
        'route': route,
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
    route = module.get('route')
    route_sha = hashlib.sha256(json.dumps(route, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    if (route_sha != module.get('routeSha256')
            or route.get('appId') != module.get('dexkit', {}).get('appId', route.get('appId'))
            or module.get('ruleCatalogSha256') != module.get('dexkit', {}).get('ruleCatalogSha256')):
        raise ValueError('Module route or catalog identity changed after preparation')


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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--app-id', required=True)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--mode', choices=('latest', 'registered'), default='latest')
    parser.add_argument('--apk-sha256', help='Select a registered APK variant by SHA-256')
    parser.add_argument('--apk-url', help='Check this exact official APK with the shared rules checker')
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
    snapshot = read_acceptance_snapshot(
        Path.cwd() / f'compatibility/{policy.app_id}/acceptance.json', policy.app_id, False)
    records = select_representatives(snapshot.records)
    if args.apk_sha256:
        records = [record for record in records
                   if record['identity']['apkSha256'].lower() == args.apk_sha256.lower()]
    if not records:
        raise ValueError('No fixed acceptance representative matches the requested APK')
    highest = max(row['identity']['versionCode'] for row in records)
    records = [row for row in records if row['identity']['versionCode'] == highest]
    if len(records) != 1:
        raise ValueError('Highest accepted APK is ambiguous; specify --apk-sha256')
    record = records[0]
    source_url = record['source']['url']
    candidate = prepare_candidate(args.app_id, source_url, args.directory)
    if candidate['identity'] != record['identity']:
        raise ValueError('Rechecked APK identity differs from the fixed acceptance record')
    digest = candidate['identity']['apkSha256']
    base_apk = args.directory / candidate['apkFiles'][0]['path']
    shutil.copy2(base_apk, args.directory / f'{policy.app_id}-{digest}.apk')
    (args.directory / 'candidate-report.json').write_text(json.dumps(candidate, indent=2) + '\n')
    write_local_runtime_assets(candidate, policy.app_id, args.directory)


if __name__ == '__main__':
    main()
