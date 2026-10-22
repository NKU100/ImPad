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

from scripts.ci.app_policy import get_policy, policy_for_report, validate_rule_changes
from scripts.ci.discover_latest_wechat import append_github_output, validate_official_apk_url
from scripts.ci.static_regression import ensure_apk, run_regression


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
    candidate_key(report)
    identity = report['identity']
    validate_official_apk_url(report.get('sourceUrl', ''), policy.official_apk_prefix)
    if (identity.get('packageName') != policy.package_name
            or identity.get('abi') not in policy.supported_abis
            or identity.get('signerSha256', '').lower() != policy.signer_sha256):
        raise ValueError('Candidate identity does not match the trusted app policy')
    if not isinstance(records, list):
        raise ValueError('Adaptation requires the validated acceptance record snapshot')
    existing = [row.get('identity', {}).get('apkSha256', '').lower()
                for row in records if isinstance(row, dict)]
    if identity['apkSha256'].lower() in existing:
        return False
    maximum = max((row.get('identity', {}).get('versionCode', 0)
                   for row in records if isinstance(row, dict)), default=0)
    if records and identity['versionCode'] <= maximum:
        from scripts.ci.acceptance import RoutingReviewRequired
        raise RoutingReviewRequired()
    from scripts.ci.acceptance import propose_forward_binding
    catalog = report.get('ruleCatalog') or {}
    if catalog.get('bindings'):
        propose_forward_binding(catalog['bindings'], records, identity['versionCode'],
                                report.get('proposedRuleId', 'task2_proposed_rule'))
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


def _rules_acceptance(repository, app_id):
    from scripts.ci.acceptance import read_acceptance_snapshot, load_rule_catalog
    path = Path(repository) / f'compatibility/{app_id}/acceptance.json'
    catalog = load_rule_catalog(app_id, repository)
    snapshot = read_acceptance_snapshot(path, app_id, False, catalog=catalog, repository=repository)
    if not snapshot.records:
        raise ValueError(f'Rule adaptation requires existing accepted records for {app_id}')
    return snapshot, catalog


def prepare_rules(repository, report_path, candidate_apk, directory):
    report = json.loads(Path(report_path).read_text())
    policy = policy_for_report(report)
    if sha256(candidate_apk) != report['identity']['apkSha256'].lower():
        raise ValueError('Candidate APK does not match the exact incompatible report')
    source_url = report.get('sourceUrl', '')
    validate_official_apk_url(source_url, policy.official_apk_prefix)
    snapshot, catalog = _rules_acceptance(repository, policy.app_id)
    if not eligible_candidate(report, snapshot.records, policy.app_id):
        raise ValueError('Candidate is not an incompatible, forward-routable rule adaptation input')
    if report.get('acceptanceSnapshotSha256') not in (None, snapshot.sha256):
        raise ValueError('Candidate report was analyzed against a different acceptance snapshot')

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
    from scripts.ci.acceptance import select_representatives
    for accepted in select_representatives(snapshot.records):
        identity = accepted['identity']
        cached = ensure_apk(accepted, Path(repository) / '.cache' / f'{policy.app_id}-apks', policy.app_id)
        old_digest = identity['apkSha256'].lower()
        destination = apk_dir / f"{identity['versionCode']}-{old_digest}.apk"
        shutil.copyfile(cached, destination)
        apks.append((identity['versionName'], str(destination.relative_to(worktree)), old_digest))

    analysis = worktree / 'work/analysis'
    analysis.mkdir(parents=True)
    shutil.copyfile(report_path, analysis / 'candidate-report.json')
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
    state = {
        'appId': policy.app_id,
        'worktree': str(worktree), 'base': base, 'agent_base': agent_base,
        'report': report, 'static': static_document,
        'acceptanceSnapshotSha256': snapshot.sha256,
        'acceptanceRecords': snapshot.records, 'baseBindings': catalog['bindings'],
        'baseCatalogs': {policy.app_id: catalog},
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
               ":app:testDebugUnitTest", ":compat-core:test", ":compat-rules:test",
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
    from scripts.ci.acceptance import propose_forward_binding
    from scripts.ci.candidate_selection import (
        prepare_candidate_from_adaptation_report, prepare_module, write_local_runtime_assets,
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

    proposed_tree = _worktree_tree(worktree, base)

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
    pending = dict(candidate_report, status='STATIC_VERIFIED_PENDING_RUNTIME')
    adaptation_report = runtime_root / 'adaptation-report.json'
    adaptation_report.write_text(json.dumps(pending, indent=2) + '\n')
    prepared = prepare_candidate_from_adaptation_report(
        policy.app_id, adaptation_report, runtime_root, repository=worktree)
    static_path = runtime_root / prepared['staticReport']
    static = json.loads(static_path.read_text())
    validate_rule_changes(base, proposed_tree, repository=worktree,
                          base_catalogs=state.get('baseCatalogs'),
                          proposed_catalogs={policy.app_id: static.get('ruleCatalog')})
    selection = static.get('selection', {})
    if selection.get('status') != 'SELECTED':
        raise ValueError('Adapted candidate did not produce one unique shared-checker plan')

    proposed_bindings = propose_forward_binding(
        state['baseBindings'], state['acceptanceRecords'], identity['versionCode'],
        selection.get('selectedRuleId', ''))
    actual_bindings = static.get('ruleCatalog', {}).get('bindings')
    sort_key = lambda row: (row.get('min', 0), row.get('ruleId', ''))
    if (not isinstance(actual_bindings, list)
            or sorted(actual_bindings, key=sort_key) != sorted(proposed_bindings, key=sort_key)):
        raise ValueError('Adapted catalog does not match the validated forward-only routing proposal')

    regression_root = runtime_root / 'regression'
    regression_rows = run_regression(
        policy.app_id, regression_root,
        worktree / f'compatibility/{policy.app_id}/acceptance.json',
        worktree / '.cache' / f'{policy.app_id}-apks', repository=worktree)
    if not regression_rows or any(row.get('status') != 'PASSED' for row in regression_rows):
        raise ValueError('REGRESSION_FAILED: one or more fixed accepted representatives failed')

    write_local_runtime_assets(prepared, policy.app_id, runtime_root)
    run_bounded(gradle_command(policy.app_id, runtime_root.relative_to(worktree)),
                worktree, dict(os.environ), directory / 'build.log', timeout=600)
    module_apk = worktree / 'app/build/outputs/apk/debug/app-debug.apk'
    prepared['module'] = prepare_module(module_apk, static, worktree)
    prepared['status'] = 'PREPARED'
    write_candidate(prepared, runtime_root)
    (worktree / 'work/analysis/candidate-report.json').write_text(
        json.dumps(prepared, indent=2) + '\n')

    if not (worktree / 'work/analysis/adaptation-summary.json').is_file():
        raise ValueError('Adaptation summary is missing from the isolated model checkout')
    from scripts.ci.adaptation_summary import read_summary
    plan = prepared['expectedPlan']
    report = {
        'appId': policy.app_id,
        'status': 'STATIC_VERIFIED_PENDING_RUNTIME',
        'identity': identity,
        'sourceUrl': candidate_report['sourceUrl'],
        'expectedPlan': plan,
        'route': prepared['route'],
        'ruleCatalogSha256': prepared['ruleCatalogSha256'],
        'acceptanceSnapshotSha256': state['acceptanceSnapshotSha256'],
        'proposedBindings': proposed_bindings,
        'checkedVersions': list(dict.fromkeys(version for version, _, _ in state['apks'])),
        'blockers': [],
        'hooks': [{'hookId': target_id, 'status': 'UNIQUE_MATCH',
                   'selectedDescriptor': target['descriptor'],
                   'candidateDescriptors': [target['descriptor']]}
                  for target_id, target in plan['targets'].items()],
        'adaptationSummary': read_summary(worktree / 'work/analysis/adaptation-summary.json'),
        'validatedPatchSha256': '',
        'validatedBaseCommit': base,
    }
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
        snapshot, _ = _rules_acceptance(args.repository.resolve(), policy.app_id)
        from scripts.ci.acceptance import RoutingReviewRequired
        try:
            eligible = eligible_candidate(report, snapshot.records, policy.app_id)
            routing_review = False
        except RoutingReviewRequired:
            eligible = False
            routing_review = True
        append_github_output(Path(os.environ["GITHUB_OUTPUT"]), {
            "adaptation_eligible": str(eligible).lower(),
            "routing_review_required": str(routing_review).lower(),
        })
        print("Candidate requires human routing review." if routing_review else
              "Codex adaptation eligible:", eligible)
    elif args.stage == "prepare":
        prepare(args.repository.resolve(), args.report, args.apk.resolve(), args.directory.resolve())
    elif args.stage == "model":
        run_model(args.directory.resolve(), args.prompt)
    else:
        validate(args.directory.resolve())


if __name__ == "__main__":
    main()
