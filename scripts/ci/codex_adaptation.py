"""Trusted controller for one Codex adaptation continuation and static validation."""

import argparse
import hashlib
import json
import os
import re
import signal
import shutil
import subprocess
import tempfile
from pathlib import Path
import shlex

from scripts.ci.app_policy import get_policy, policy_for_report
from scripts.ci.discover_latest_wechat import append_github_output
from scripts.ci.download_url import validate_download_url
from scripts.ci.static_regression import ensure_apk, record_apk_inputs, run_regression
from scripts.ci.apk_inputs import resolve_apk_inputs


MODEL = "gpt-6-luna"
REASONING = "xhigh"


def candidate_key(report):
    policy = policy_for_report(report)
    identity = report["identity"]
    code, digest = identity["versionCode"], identity["apkSha256"]
    if type(code) is not int or code <= 0 or not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest, re.IGNORECASE):
        raise ValueError("Candidate version code or SHA-256 is invalid")
    if identity.get("abi") not in policy.supported_abis:
        raise ValueError("Candidate ABI is not registered for this app")
    return f"{policy.app_id}-{identity['packageName']}-{identity['abi']}-{code}-{digest.lower()}"


def eligible_candidate(report, records, app_id=None):
    policy = policy_for_report(report, app_id)
    if report.get('status') != 'INCOMPATIBLE':
        return False
    from scripts.ci.semantic_protocol import validate_catalog, validate_snapshot
    if report.get('schemaVersion') != 2:
        raise ValueError('Adaptation requires a semantic schema-2 candidate')
    catalog = report.get('ruleCatalog')
    validate_catalog(catalog, policy.app_id)
    validate_snapshot(report.get('snapshot'))
    recognition = report.get('recognition')
    expected_recognition = {'appId': policy.app_id,
        'evaluatedRecognizerIds': recognition.get('evaluatedRecognizerIds') if isinstance(recognition, dict) else None,
        'ruleCatalogSha256': catalog['ruleCatalogSha256']}
    recognition_sha = hashlib.sha256(json.dumps(
        expected_recognition, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    if (recognition != expected_recognition or recognition_sha != report.get('recognitionSha256')
            or report.get('ruleCatalogSha256') != catalog['ruleCatalogSha256']
            or not isinstance(report.get('diagnostic'), dict)):
        raise ValueError('Candidate semantic recognition identity is invalid')
    candidate_key(report)
    identity = report['identity']
    validate_download_url(report.get('sourceUrl', ''))
    if (identity.get('packageName') != policy.package_name
            or identity.get('abi') not in policy.supported_abis
            or identity.get('signerSha256', '').lower() != policy.signer_sha256):
        raise ValueError('Candidate identity does not match the trusted app policy')
    if not isinstance(records, list):
        raise ValueError('Adaptation requires the validated acceptance record snapshot')
    candidate_inputs = report.get('apkFiles')
    candidate_hashes = ([row.get('sha256', '').lower() for row in candidate_inputs]
                        if isinstance(candidate_inputs, list) and candidate_inputs else
                        [identity['apkSha256'].lower()])
    existing = []
    for row in records:
        if not isinstance(row, dict):
            continue
        case_identity = row.get('identity', {})
        case_files = row.get('apkFiles')
        hashes = ([item.get('sha256', '').lower() for item in case_files]
                  if isinstance(case_files, list) and case_files else
                  [case_identity.get('apkSha256', '').lower()])
        existing.append((case_identity.get('apkSha256', '').lower(), hashes))
    if (identity['apkSha256'].lower(), candidate_hashes) in existing:
        return False
    return True


def validate_paths(paths, app_id="wechat"):
    policy = get_policy(app_id)
    app_test_prefix = policy.app_source_prefix.replace("/src/main/kotlin/", "/src/test/kotlin/", 1)
    shared_test_prefixes = [app_test_prefix]
    for source in policy.shared_source_paths:
        if source.startswith("compat-core/src/main/kotlin/"):
            shared_test_prefixes.append(source.split("/src/main/kotlin/", 1)[0] + "/src/test/kotlin/")
    rule_main_prefix = f"compat-rules/src/main/kotlin/io/github/nku100/impad/rules/{policy.app_id}/"
    rule_test_prefix = f"compat-rules/src/test/kotlin/io/github/nku100/impad/rules/{policy.app_id}/"
    shared_catalog = 'compat-rules/src/main/kotlin/io/github/nku100/impad/rules/RuleCatalog.kt'
    allowed_exact = set(policy.shared_source_paths) | {shared_catalog}
    allowed_prefixes = (policy.app_source_prefix, *shared_test_prefixes)
    allowed_prefixes += (rule_main_prefix, rule_test_prefix)
    for path in paths:
        if (path not in allowed_exact
                and not path.startswith(allowed_prefixes)):
            raise ValueError(f"Agent edit is outside the allowed adaptation paths: {path}")


def _worktree_tree(worktree, base):
    with tempfile.TemporaryDirectory(prefix='impad-proposed-tree-') as temporary:
        env = dict(os.environ, GIT_INDEX_FILE=str(Path(temporary) / 'index'))
        subprocess.run(['git', 'read-tree', base], cwd=worktree, env=env, check=True)
        subprocess.run(['git', 'add', '-A'], cwd=worktree, env=env, check=True)
        return git(worktree, 'write-tree', env=env)


def _load_semantic_catalog(app_id, repository):
    from scripts.ci.semantic_protocol import validate_catalog
    repository = Path(repository).resolve()
    with tempfile.TemporaryDirectory(prefix='impad-semantic-catalog-') as temporary:
        report = Path(temporary) / 'catalog.json'
        arguments = shlex.join(['catalog', '--app-id', app_id, '--report', str(report)])
        subprocess.run(['./gradlew', '--no-daemon', ':compat-checker:run', '--args=' + arguments],
            cwd=repository, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=True, timeout=300)
        catalog = json.loads(report.read_text())
    validate_catalog(catalog, app_id)
    return catalog


def _rules_acceptance(repository, app_id):
    from scripts.ci.semantic_regression_baseline import load_semantic_regression_baseline
    baseline = load_semantic_regression_baseline(repository, app_id)
    baseline.assert_unchanged()
    if not baseline.cases:
        raise ValueError(f'Rule adaptation requires existing accepted records for {app_id}')
    catalog = _load_semantic_catalog(app_id, repository)
    baseline.assert_unchanged()
    return baseline.migration_bundle, catalog, baseline


def _validate_candidate_static(report, artifact_root, expected_catalog=None):
    from scripts.ci.candidate_selection import recognition_identity
    from scripts.ci.semantic_protocol import validate_check_report
    artifact_root = Path(artifact_root).resolve()
    relative = Path(report.get('staticReport', ''))
    if relative.is_absolute() or not relative.parts or '..' in relative.parts:
        raise ValueError('Candidate static report path is malformed')
    static_path = (artifact_root / relative).resolve()
    static_path.relative_to(artifact_root)
    if (not static_path.is_file()
            or sha256(static_path) != report.get('staticReportSha256')):
        raise ValueError('Incompatible static report changed after candidate analysis')
    static = json.loads(static_path.read_text())
    validate_check_report(static, expected_app_id=report['appId'], expected_identity=report['identity'])
    recognition, digest = recognition_identity(static)
    if (static['selection']['status'] == 'SELECTED'
            or report.get('diagnostic', {}).get('selection') != static['selection']['status']
            or report.get('ruleCatalog') != static['ruleCatalog']
            or report.get('snapshot') != static['snapshot']
            or report.get('ruleCatalogSha256') != static['ruleCatalog']['ruleCatalogSha256']
            or report.get('recognition') != recognition
            or report.get('recognitionSha256') != digest
            or (expected_catalog is not None and static['ruleCatalog'] != expected_catalog)):
        raise ValueError('Candidate and static semantic identities differ')
    candidate_inputs = report.get('apkFiles')
    if (not isinstance(candidate_inputs, list) or len(candidate_inputs) != len(static['inputHashes'])
            or [(row.get('path'), row.get('sha256', '').lower()) for row in candidate_inputs]
            != [(row['path'], row['sha256'].lower()) for row in static['inputHashes']]):
        raise ValueError('Candidate APK inputs differ from the static report inputs')
    return static_path, static


def _validate_frozen_oracle(directory, state, policy):
    bundle, _catalog, baseline = _rules_acceptance(directory, policy.app_id)
    if (bundle.history_sha256 != state['migrationHistorySha256']
            or bundle.expectations_sha256 != state['migrationExpectationsSha256']):
        raise ValueError('Frozen semantic migration oracle changed during adaptation')
    profile = next((row for row in bundle.profiles if row['appId'] == policy.app_id), None)
    if profile is None or profile['historicalAcceptanceSha256'] != state['acceptanceSnapshotSha256']:
        raise ValueError('Original acceptance snapshot changed during adaptation')
    baseline_binding = {
        'sha256': baseline.sha256,
        'appId': baseline.app_id,
        'migrationHistorySha256': bundle.history_sha256,
        'migrationExpectationsSha256': bundle.expectations_sha256,
        'v2Path': baseline.v2_path.relative_to(Path(directory).resolve()).as_posix(),
        'v2SnapshotSha256': baseline.v2_sha256,
        'regressionRecordIds': [case['recordId'] for case in baseline.cases],
        'sourceCases': [{'kind': case['kind'], 'recordId': case['recordId'],
                         'profileId': case['profileId'],
                         'sourceSha256': case['acceptanceSnapshotSha256']}
                        for case in baseline.cases],
    }
    if (baseline.sha256 != state['baselineSha256']
            or baseline.v2_sha256 != state['v2SnapshotSha256']
            or [case['recordId'] for case in baseline.cases] != state['regressionRecordIds']
            or baseline_binding != state.get('regressionBaseline')):
        raise ValueError('Semantic regression baseline changed during adaptation')
    baseline.assert_unchanged()
    return bundle


def _validate_frozen_oracle_tree(repository, base, proposed_tree, app_id,
                                 expected_v2_sha256=None):
    history_path = Path(repository) / 'compatibility/semantic-migration/history.json'
    expectations_path = Path(repository) / 'compatibility/semantic-migration/expectations.json'
    history = json.loads(history_path.read_text())
    expectations = json.loads(expectations_path.read_text())
    if app_id not in {table['appId'] for table in history['tables']}:
        raise ValueError(f'Historical migration has no acceptance table for {app_id}')
    paths = {
        *(table['path'] for table in history['tables']),
        'compatibility/semantic-migration/history.json',
        'compatibility/semantic-migration/expectations.json',
        expectations['analysisEvidence']['path'],
        *(row['catalog']['path'] for table in history['tables'] for row in table['records']),
    }
    for revision in (base, proposed_tree):
        for path in paths:
            subprocess.run(['git', 'cat-file', '-e', f'{revision}:{path}'], cwd=repository,
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=True)
    for path in paths:
        baseline = subprocess.check_output(['git', 'show', f'{base}:{path}'], cwd=repository)
        proposed = subprocess.check_output(['git', 'show', f'{proposed_tree}:{path}'], cwd=repository)
        if proposed != baseline:
            raise ValueError(f'Frozen acceptance or semantic migration artifact changed: {path}')
    v2_path = f'compatibility/{app_id}/acceptance-v2.json'
    base_has_v2 = subprocess.run(['git', 'cat-file', '-e', f'{base}:{v2_path}'], cwd=repository,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    proposed_has_v2 = subprocess.run(['git', 'cat-file', '-e', f'{proposed_tree}:{v2_path}'], cwd=repository,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    if base_has_v2 != proposed_has_v2:
        raise ValueError('Accepted-v2 table presence changed in the proposed adaptation tree')
    base_v2 = (subprocess.check_output(['git', 'show', f'{base}:{v2_path}'], cwd=repository)
               if base_has_v2 else None)
    proposed_v2 = (subprocess.check_output(['git', 'show', f'{proposed_tree}:{v2_path}'], cwd=repository)
                   if proposed_has_v2 else None)
    actual_v2_sha256 = hashlib.sha256(base_v2).hexdigest() if base_v2 is not None else None
    if actual_v2_sha256 != expected_v2_sha256 or proposed_v2 != base_v2:
        raise ValueError('Accepted-v2 table bytes changed in the proposed adaptation tree')


def copy_runtime_failure(report, artifact_root, analysis):
    failure = report.get('runtimeFailure')
    if failure is None:
        return
    if failure.get('outcome') != 'RUNTIME_INCOMPATIBLE' or not failure.get('evidence'):
        raise ValueError('Runtime failure export is incomplete')
    artifact_root = Path(artifact_root).resolve()
    for item in failure['evidence']:
        relative = Path(item['path'])
        if (relative.is_absolute() or '..' in relative.parts
                or not re.fullmatch(r'[a-f0-9]{64}', str(item.get('sha256', '')))):
            raise ValueError('Runtime failure evidence reference is malformed')
        source = (artifact_root / relative).resolve()
        source.relative_to(artifact_root)
        if not source.is_file() or sha256(source) != item['sha256']:
            raise ValueError('Runtime failure evidence changed before adaptation')
        destination = Path(analysis) / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)


def prepare_rules(repository, report_path, candidate_apk, directory):
    report = json.loads(Path(report_path).read_text())
    policy = policy_for_report(report)
    if sha256(candidate_apk) != report['identity']['apkSha256'].lower():
        raise ValueError('Candidate APK does not match the exact incompatible report')
    source_url = report.get('sourceUrl', '')
    validate_download_url(source_url)
    bundle, catalog, baseline = _rules_acceptance(repository, policy.app_id)
    records = [dict(case, source=dict(case['source'], apkFiles=case['apkFiles']))
               for case in baseline.cases]
    if not eligible_candidate(report, records, policy.app_id):
        raise ValueError('Candidate is not an incompatible semantic adaptation input')
    profile = next((row for row in bundle.profiles if row['appId'] == policy.app_id), None)
    if profile is None:
        raise ValueError(f'Frozen semantic expectations are missing for {policy.app_id}')
    if report.get('acceptanceSnapshotSha256') not in (None, profile['historicalAcceptanceSha256']):
        raise ValueError('Candidate report was analyzed against a different acceptance snapshot')
    static_path, static_document = _validate_candidate_static(
        report, Path(report_path).resolve().parent, expected_catalog=catalog)

    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    worktree = directory / 'checkout'
    base = git(repository, 'rev-parse', 'HEAD')
    subprocess.run(['git', 'clone', '--quiet', '--no-local', '--no-checkout',
                    str(Path(repository).resolve()), str(worktree)], check=True)
    subprocess.run(['git', 'remote', 'remove', 'origin'], cwd=worktree, check=True)
    subprocess.run(['git', 'checkout', '--quiet', '--detach', base], cwd=worktree, check=True)
    agent_base = git(worktree, 'rev-parse', 'HEAD')

    apk_dir = worktree / 'work/apks'
    apk_dir.mkdir(parents=True)
    digest = report['identity']['apkSha256'].lower()
    candidate_filename = f'candidate-{digest}.apk'
    shutil.copyfile(candidate_apk, apk_dir / candidate_filename)
    apks = [(report['identity']['versionName'], f'work/apks/{candidate_filename}', digest)]
    for accepted in records:
        identity = accepted['identity']
        cached = ensure_apk(accepted, Path(repository) / '.cache' / f'{policy.app_id}-apks', policy.app_id)
        old_digest = identity['apkSha256'].lower()
        destination = apk_dir / f"{identity['versionCode']}-{old_digest}.apk"
        shutil.copyfile(cached, destination)
        apks.append((identity['versionName'], str(destination.relative_to(worktree)), old_digest))
        for split in record_apk_inputs(accepted, Path(repository) / '.cache' / f'{policy.app_id}-apks', cached)[1:]:
            shutil.copyfile(split, apk_dir / split.name)

    analysis = worktree / 'work/analysis'
    analysis.mkdir(parents=True)
    shutil.copyfile(report_path, analysis / 'candidate-report.json')
    copy_runtime_failure(report, Path(report_path).parent, analysis)
    static_ref = report.get('staticReport')
    static_path = Path(report_path).parent / static_ref if isinstance(static_ref, str) else None
    if static_path is None or not static_path.is_file():
        raise ValueError('Incompatible report has no saved raw static-checker result')
    if hashlib.sha256(static_path.read_bytes()).hexdigest() != report.get('staticReportSha256'):
        raise ValueError('Incompatible static report changed after candidate analysis')
    shutil.copyfile(static_path, analysis / 'static-check.json')
    artifact_root = Path(report_path).resolve().parent
    apk_files = report.get('apkFiles')
    if not isinstance(apk_files, list) or not apk_files:
        raise ValueError('Incompatible report has no exact base/split APK identities')
    for index, item in enumerate(apk_files):
        relative = Path(item.get('path', '')) if isinstance(item, dict) else Path('/')
        if (relative.is_absolute() or not relative.parts or '..' in relative.parts
                or item.get('role') != ('base' if index == 0 else 'split')
                or not re.fullmatch(r'[a-fA-F0-9]{64}', str(item.get('sha256', '')))):
            raise ValueError('Incompatible APK file identity is malformed')
        source = (artifact_root / relative).resolve()
        try:
            source.relative_to(artifact_root)
        except ValueError as error:
            raise ValueError('Incompatible APK input escapes its exact artifact') from error
        if not source.is_file() or sha256(source) != item['sha256'].lower():
            raise ValueError('Incompatible APK input changed after static analysis')
        destination = analysis / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        if index == 0 and item['sha256'].lower() != digest:
            raise ValueError('Incompatible report base APK differs from the selected candidate')
    static_document = json.loads(static_path.read_text())
    for row in static_document.get('inputHashes', []):
        if not isinstance(row, dict) or not isinstance(row.get('path'), str):
            raise ValueError('Incompatible static report has a malformed input reference')
        input_path = (artifact_root / row['path']).resolve()
        try:
            input_path.relative_to(artifact_root)
        except ValueError as error:
            raise ValueError('Incompatible static report input escapes its exact artifact') from error
        if not input_path.is_file() or sha256(input_path) != row.get('sha256', '').lower():
            raise ValueError('Incompatible static report input changed after analysis')
    baseline.assert_unchanged()
    app_records = records
    state = {
        'appId': policy.app_id,
        'worktree': str(worktree), 'base': base, 'agent_base': agent_base,
        'report': report, 'static': static_document,
        'acceptanceSnapshotSha256': profile['historicalAcceptanceSha256'],
        'migrationHistorySha256': bundle.history_sha256,
        'migrationExpectationsSha256': bundle.expectations_sha256,
        'baselineSha256': baseline.sha256,
        'v2SnapshotSha256': baseline.v2_sha256,
        'regressionRecordIds': [case['recordId'] for case in baseline.cases],
        'regressionBaseline': {
            'sha256': baseline.sha256,
            'appId': baseline.app_id,
            'migrationHistorySha256': bundle.history_sha256,
            'migrationExpectationsSha256': bundle.expectations_sha256,
            'v2Path': baseline.v2_path.relative_to(Path(repository).resolve()).as_posix(),
            'v2SnapshotSha256': baseline.v2_sha256,
            'regressionRecordIds': [case['recordId'] for case in baseline.cases],
            'sourceCases': [{'kind': case['kind'], 'recordId': case['recordId'],
                             'profileId': case['profileId'],
                             'sourceSha256': case['acceptanceSnapshotSha256']}
                            for case in baseline.cases],
        },
        'acceptanceRecords': app_records,
        'apks': apks,
        'existing_tests': {str(path.relative_to(worktree)): sha256(path)
                           for module in ('app', 'compat-core', 'compat-rules', 'compat-checker')
                           for path in (worktree / module / 'src/test').rglob('*') if path.is_file()},
    }
    (directory / 'state.json').write_text(json.dumps(state, indent=2))
    (directory / 'candidate-report.json').write_text(json.dumps(report, indent=2) + '\n')


def run_bounded(command, cwd, env, log, timeout=None):
    with log.open("w") as output:
        process = subprocess.Popen(command, cwd=cwd, env=env, stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            result = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            raise TimeoutError(f"Command exceeded its {timeout}-second execution limit") from None
        except BaseException:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            raise
    if result:
        raise RuntimeError(f"Command failed with exit code {result}; raw output is retained only on the runner")


def gradle_command(app_id, runtime_assets=None):
    command = ["./gradlew", "--offline", "--no-daemon",
               ":app:testDebugUnitTest", ":app:testReleaseUnitTest",
               ":compat-core:test", ":compat-rules:test",
               ":compat-checker:test", ":app:verifyModuleMetadata", ":app:assembleDebug"]
    if app_id == 'wecom':
        command[3:3] = ['--no-parallel', '--max-workers=2']
    if runtime_assets is not None:
        command.insert(3, f'-PruntimeLocalAssets={Path(runtime_assets).as_posix()}')
    return command


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def validate_existing_tests(worktree, existing_tests):
    for path, digest in existing_tests.items():
        if not (worktree / path).is_file() or sha256(worktree / path) != digest:
            raise ValueError("Existing regression tests must remain unchanged")


def git(cwd, *arguments, env=None):
    return subprocess.check_output(["git", *arguments], cwd=cwd, env=env).decode().strip()


def patch_tree(checkout, base, patch_path):
    """Reconstruct a checkpoint tree without changing the checkout or its index."""
    with tempfile.TemporaryDirectory() as temporary:
        env = dict(os.environ, GIT_INDEX_FILE=str(Path(temporary) / 'index'))
        subprocess.run(['git', 'read-tree', base], cwd=checkout, env=env, check=True)
        if Path(patch_path).stat().st_size:
            subprocess.run(['git', 'apply', '--cached', str(Path(patch_path).resolve())],
                           cwd=checkout, env=env, check=True, stderr=subprocess.PIPE)
        return subprocess.check_output(['git', 'write-tree'], cwd=checkout, env=env).decode().strip()


def prepare(repository, report_path, candidate_apk, directory):
    return prepare_rules(repository, report_path, candidate_apk, directory)


def run_model(directory, prompt_path):
    from scripts.ci.codex_goal import run_model_goal
    run_model_goal(directory, prompt_path)


def unverified_report(report):
    result = {key: value for key, value in report.items()
              if key not in {'validatedPatchSha256', 'validatedBaseCommit'}}
    if report.get('status') not in {'INCOMPATIBLE', 'ROUTING_REVIEW_REQUIRED'}:
        result['status'] = 'NEEDS_HOOK_REVIEW'
    return result


def _validate_rules(directory, state, policy, worktree):
    from scripts.ci.candidate_selection import (
        prepare_candidate_from_adaptation_report, prepare_module, recognition_identity,
        write_local_runtime_assets,
    )
    from scripts.ci.redroid_prepare_apps import write_candidate

    base = state.get('agent_base', state['base'])
    if git(worktree, 'rev-parse', 'HEAD') != base:
        raise ValueError('Agent must leave adaptation changes uncommitted')
    tracked = git(worktree, 'diff', '--name-only', base).splitlines()
    new_files = git(worktree, 'ls-files', '--others', '--exclude-standard').splitlines()
    paths = tracked + new_files
    validate_paths(paths, policy.app_id)
    if any((worktree / path).is_symlink() for path in paths):
        raise ValueError('Adaptation files must not be symbolic links')
    validate_existing_tests(worktree, state['existing_tests'])
    bundle = _validate_frozen_oracle(worktree, state, policy)

    proposed_tree = _worktree_tree(worktree, base)
    _validate_frozen_oracle_tree(worktree, base, proposed_tree, policy.app_id,
                                 state['v2SnapshotSha256'])

    candidate_report = dict(state['report'])
    if candidate_report.get('status') != 'INCOMPATIBLE':
        raise ValueError('Model adaptation must start from a shared-checker incompatible candidate')
    identity = candidate_report['identity']
    candidate_digest = identity['apkSha256'].lower()
    candidate_apk = worktree / 'work/apks' / f'candidate-{candidate_digest}.apk'
    if not candidate_apk.is_file() or sha256(candidate_apk) != candidate_digest:
        raise ValueError('Exact candidate APK changed during the model adaptation')

    runtime_root = worktree / 'work/runtime-smoke'
    runtime_root.mkdir(parents=True, exist_ok=True)
    staged_apk = runtime_root / f'{policy.app_id}-{candidate_digest}.apk'
    shutil.copyfile(candidate_apk, staged_apk)
    analysis_root = worktree / 'work/analysis'
    exact_inputs = resolve_apk_inputs(candidate_report, analysis_root) if candidate_report.get('apkFiles') else []
    for item, path in zip(candidate_report.get('apkFiles', []), exact_inputs):
        destination = runtime_root / item['path']
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)
    pending = dict(candidate_report, status='STATIC_VERIFIED_PENDING_RUNTIME')
    adaptation_report = runtime_root / 'adaptation-report.json'
    adaptation_report.write_text(json.dumps(pending, indent=2) + '\n')
    prepared = prepare_candidate_from_adaptation_report(
        policy.app_id, adaptation_report, runtime_root, repository=worktree)
    static_path = runtime_root / prepared['staticReport']
    static = json.loads(static_path.read_text())
    from scripts.ci.semantic_protocol import validate_check_report
    validate_check_report(static, expected_app_id=policy.app_id, expected_identity=identity)
    selection = static.get('selection', {})
    recognition, recognition_sha = recognition_identity(static)
    if selection.get('status') != 'SELECTED':
        raise ValueError('Adapted candidate did not produce one selected complete semantic plan')
    if (prepared.get('expectedPlan') != selection.get('plan')
            or prepared.get('expectedExecutionSha256') != selection.get('executionSha256')
            or prepared.get('ruleCatalog') != static.get('ruleCatalog')
            or prepared.get('snapshot') != static.get('snapshot')
            or prepared.get('recognition') != recognition
            or prepared.get('recognitionSha256') != recognition_sha):
        raise ValueError('Prepared candidate differs from the checker complete semantic plan')

    regression_root = runtime_root / 'regression'
    regression_rows = run_regression(
        policy.app_id, regression_root,
        cache=worktree / '.cache' / f'{policy.app_id}-apks', repository=worktree)
    expected_cases = state['regressionBaseline']['sourceCases']
    if (not regression_rows or any(row.get('status') != 'PASSED' for row in regression_rows)
            or [row.get('recordId') for row in regression_rows] != state['regressionRecordIds']
            or any(row.get('baselineSha256') != state['baselineSha256']
                   or row.get('v2SnapshotSha256') != state['v2SnapshotSha256']
                   or row.get('baselineSourceCase') != expected
                   for row, expected in zip(regression_rows, expected_cases))):
        raise ValueError('REGRESSION_FAILED: one or more fixed accepted representatives failed')
    _validate_frozen_oracle(worktree, state, policy)

    write_local_runtime_assets(prepared, policy.app_id, runtime_root)
    run_bounded(gradle_command(policy.app_id, runtime_root.relative_to(worktree)),
                worktree, dict(os.environ), directory / 'build.log', timeout=600)
    module_apk = worktree / 'app/build/outputs/apk/debug/app-debug.apk'
    prepared['module'] = prepare_module(module_apk, static, worktree)
    prepared['status'] = 'PREPARED'
    write_candidate(prepared, runtime_root)
    candidate_report_path = runtime_root / 'candidate-report.json'
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=runtime_root,
                                     prefix='.candidate-report-', suffix='.tmp', delete=False) as temporary:
        temporary.write(json.dumps(prepared, indent=2) + '\n')
        temporary_report_path = Path(temporary.name)
    temporary_report_path.replace(candidate_report_path)
    (worktree / 'work/analysis/candidate-report.json').write_text(
        json.dumps(prepared, indent=2) + '\n')

    if not (worktree / 'work/analysis/adaptation-summary.json').is_file():
        raise ValueError('Adaptation summary is missing from the isolated model checkout')
    from scripts.ci.adaptation_summary import read_summary
    plan = prepared['expectedPlan']
    report = {
        'schemaVersion': 2,
        'appId': policy.app_id,
        'status': 'STATIC_VERIFIED_PENDING_RUNTIME',
        'identity': identity,
        'sourceUrl': candidate_report['sourceUrl'],
        'apkFiles': prepared['apkFiles'],
        'expectedPlan': plan,
        'expectedExecutionSha256': prepared['expectedExecutionSha256'],
        'ruleCatalog': prepared['ruleCatalog'],
        'snapshot': prepared['snapshot'],
        'recognition': prepared['recognition'],
        'recognitionSha256': prepared['recognitionSha256'],
        'ruleCatalogSha256': prepared['ruleCatalogSha256'],
        'acceptanceSnapshotSha256': state['acceptanceSnapshotSha256'],
        'baselineSha256': state['baselineSha256'],
        'v2SnapshotSha256': state['v2SnapshotSha256'],
        'regressionRecordIds': state['regressionRecordIds'],
        'regressionBaseline': state['regressionBaseline'],
        'checkedVersions': list(dict.fromkeys(version for version, _, _ in state['apks'])),
        'blockers': [],
        'hooks': [{'hookId': hook['role'], 'status': 'UNIQUE_MATCH',
                   'selectedDescriptor': hook['descriptor'],
                   'candidateDescriptors': [hook['descriptor']]}
                  for hook in plan['hooks']],
        'adaptationSummary': read_summary(worktree / 'work/analysis/adaptation-summary.json'),
        'validatedPatchSha256': '',
        'validatedBaseCommit': base,
    }
    if candidate_report.get('sourceArchiveSha256'):
        report['sourceArchiveSha256'] = candidate_report['sourceArchiveSha256']
    for path in new_files:
        subprocess.run(['git', 'add', '--', path], cwd=worktree, check=True)
    subprocess.run(['git', 'diff', '--check', base], cwd=worktree, check=True)
    with (directory / 'adaptation.patch').open('wb') as patch:
        subprocess.run(['git', 'diff', '--binary', '--full-index', base], cwd=worktree,
                       stdout=patch, check=True)
    report['validatedPatchSha256'] = sha256(directory / 'adaptation.patch')
    report['validatedBaseCommit'] = base
    (directory / 'candidate-report.json').write_text(json.dumps(report, indent=2) + '\n')


def validate(directory):
    state = json.loads((directory / "state.json").read_text())
    (directory / "candidate-report.json").write_text(json.dumps(unverified_report(state['report']), indent=2) + '\n')
    policy = get_policy(state.get("appId"))
    worktree = Path(state["worktree"])
    _validate_rules(directory, state, policy, worktree)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=["eligibility", "prepare", "model", "validate"])
    parser.add_argument("--app-id", required=True)
    parser.add_argument("--repository", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--apk", type=Path)
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--prompt", type=Path)
    args = parser.parse_args()
    if args.stage == "eligibility":
        report = json.loads(args.report.read_text())
        policy = get_policy(args.app_id)
        bundle, _ = _rules_acceptance(args.repository.resolve(), policy.app_id)
        _validate_candidate_static(report, args.report.resolve().parent)
        records = [row for row in bundle.historical_records if row.get('appId') == policy.app_id]
        eligible = eligible_candidate(report, records, policy.app_id)
        append_github_output(Path(os.environ["GITHUB_OUTPUT"]), {
            "adaptation_eligible": str(eligible).lower(),
            "routing_review_required": "false",
        })
        print("Codex adaptation eligible:", eligible)
    elif args.stage == "prepare":
        prepare(args.repository.resolve(), args.report, args.apk.resolve(), args.directory.resolve())
    elif args.stage == "model":
        run_model(args.directory.resolve(), args.prompt)
    else:
        validate(args.directory.resolve())


if __name__ == "__main__":
    main()
