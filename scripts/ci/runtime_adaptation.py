"""Attach a disposable ReDroid and independently validate every adaptation turn."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import uuid

from scripts.ci import runtime_smoke as smoke
from scripts.ci.app_policy import get_policy
from scripts.ci.codex_adaptation import sha256
from scripts.ci.redroid_environment_probe import Probe, SERIAL
from scripts.ci.redroid_app_smoke import AppSmoke, query_target
from scripts.ci.candidate_selection import prepare_module, verify_module_identity
from scripts.ci.runtime_task import evaluate_validation
from scripts.ci.static_regression import run_regression
from scripts.ci.adb_relay import normalize_adb_server_socket


def configure(directory):
    state = json.loads((directory / 'state.json').read_text())
    app_id = state.get('appId', state.get('report', {}).get('appId'))
    policy = get_policy(app_id)
    smoke.configure_app_id(policy.app_id)
    root = Path(state['runtime']['root'])
    os.chdir(Path(state['worktree']).resolve())
    smoke.ROOT = root
    os.environ['ADB_SERVER_SOCKET'] = normalize_adb_server_socket(state['runtime']['socket'])
    os.environ['ANDROID_SERIAL'] = state['runtime']['serial']
    return state, root


def initialize(directory, root, serial=SERIAL):
    devices = subprocess.check_output([os.environ.get('IMPAD_ADB_BINARY', 'adb'), 'devices'], text=True).splitlines()[1:]
    connected = [line.split()[0] for line in devices if line.strip()]
    if connected != [serial] or serial != SERIAL:
        raise RuntimeError('Runtime adaptation requires exactly one disposable ReDroid')
    state_path = directory / 'state.json'
    state = json.loads(state_path.read_text())
    policy = get_policy(state.get('appId', state.get('report', {}).get('appId')))
    smoke.configure_app_id(policy.app_id)
    workspace = Path(state['worktree'])
    candidate_apk = root / f'{policy.app_id}.apk'
    if sha256(candidate_apk) != state['report']['identity']['apkSha256']:
        raise ValueError('Runtime candidate does not match verified identity')
    os.environ['ANDROID_SERIAL'] = serial
    query_target()
    candidate = dict(state['report'], appId=policy.app_id)
    inputs = {policy.app_id: {'identity': candidate['identity'], 'signerVerified': True}}
    if policy.app_id == 'qq':
        from scripts.ci.apps.qq.runtime_probe import manifest_expectation
        inputs[policy.app_id]['requestIdentityExpectation'] = manifest_expectation(candidate_apk, candidate['identity']['apkSha256'])
    (root / 'inputs.json').write_text(json.dumps(inputs, indent=2))
    (root / 'candidate-report.json').write_text(json.dumps(candidate, indent=2))
    framework = Probe(root / 'framework')
    app = AppSmoke(root, framework)
    app.setup()
    smoke.configure_app_id(policy.app_id)
    smoke.EVIDENCE = root / 'prepare-evidence'
    smoke.EVIDENCE.mkdir(exist_ok=True)
    smoke.configure_manager(False)
    smoke.adb('shell', 'pm', 'clear', policy.package_name)
    ui = smoke.mobile_input('baseline-mobile')
    if any(smoke.is_tablet_entry(node.get('text', '')) for node in smoke.nodes(ui)):
        raise RuntimeError('ReDroid baseline already has a tablet entry')
    (root / 'prepared.json').write_text(json.dumps({'appId': policy.app_id, 'baseline': policy.runtime_baseline}))
    smoke.configure_manager(True)
    address = normalize_adb_server_socket(f'localfilesystem:.impad-adb-{uuid.uuid4().hex}.sock')
    if len(os.fsencode(address.removeprefix('localfilesystem:'))) >= 108:
        raise ValueError('Private ADB endpoint exceeds the Unix socket pathname limit')
    os.chdir(workspace)
    env = dict(os.environ, ADB_SERVER_SOCKET=address, ANDROID_SERIAL=serial)
    state['runtime'] = {'root': str(root), 'serial': serial, 'socket': address,
                        'serverOwned': True, 'environment': 'redroid'}
    state_path.write_text(json.dumps(state, indent=2))
    adb = os.environ.get('IMPAD_ADB_BINARY', 'adb')
    try:
        # Use a task-private server for the sandbox while leaving any inherited server untouched.
        subprocess.run([adb, '-L', address, 'start-server'], env=env, cwd=workspace, check=True, timeout=30)
        subprocess.run([adb, 'connect', serial], env=env, cwd=workspace, check=True, timeout=30)
        subprocess.run([adb, '-s', serial, 'wait-for-device'], env=env, cwd=workspace, check=True, timeout=120)
        devices = subprocess.check_output([adb, 'devices'], env=env, cwd=workspace, text=True).splitlines()[1:]
        connected = [line.split()[0] for line in devices if line.strip()]
        if connected != [serial]:
            raise RuntimeError('The model ADB server must expose exactly the dedicated ReDroid')
    except Exception:
        stopped = subprocess.run([adb, '-L', address, 'kill-server'], env=env, cwd=workspace,
                                 check=False, timeout=30)
        state['runtime']['serverOwned'] = stopped.returncode != 0
        state_path.write_text(json.dumps(state, indent=2))
        raise
    runtime_instructions = workspace / 'work/analysis/runtime-device.json'
    runtime_instructions.parent.mkdir(parents=True, exist_ok=True)
    runtime_instructions.write_text(json.dumps({
        'appId': policy.app_id, 'serial': serial, 'baseline': policy.runtime_baseline,
        'instructions': f'Run adb app-launch to restore the managed runtime configuration and start {policy.package_name}. After adding or refining the shared rule, run impad-build for guarded host checks/tests/build; do not retry Gradle inside the network-disabled sandbox. Build logs are under work/analysis/host-build. Use adb device commands to inspect the disposable ReDroid. Interrupted requests are cancelled by the relay. Independent runtime verification runs after every completed turn.'}, indent=2))


def stop_owned_adb_server(directory):
    state_path = Path(directory) / 'state.json'
    if not state_path.is_file():
        return False
    state = json.loads(state_path.read_text())
    runtime = state.get('runtime', {})
    if runtime.get('serverOwned') is not True:
        return False
    endpoint = normalize_adb_server_socket(runtime.get('socket'))
    env = dict(os.environ, ADB_SERVER_SOCKET=endpoint)
    workspace = Path(state['worktree']).resolve()
    result = subprocess.run([os.environ.get('IMPAD_ADB_BINARY', 'adb'), '-L', endpoint, 'kill-server'],
                            env=env, cwd=workspace, check=False, timeout=30)
    if result.returncode:
        raise RuntimeError(f'Owned ADB server failed to stop at {endpoint}')
    return True


def probe_candidate(root, evidence, candidate):
    app = AppSmoke(root, Probe(root / 'framework'))
    app.probe.output = evidence
    smoke.configure_app_id(candidate['appId'])
    try:
        report = app.run_candidate(candidate)
    finally:
        smoke.EVIDENCE = evidence
    shutil.copyfile(evidence / 'evidence/smoke-report.json', evidence / 'smoke-report.json')
    return report


def verify_runtime(directory):
    state, root = configure(directory)
    policy = get_policy(state.get('appId', state.get('report', {}).get('appId')))
    smoke.configure_app_id(policy.app_id)
    workspace = Path(state['worktree'])
    apk = workspace / 'app/build/outputs/apk/debug/app-debug.apk'
    iteration = len(list(root.glob('iteration-*'))) + 1
    smoke.EVIDENCE = root / f'iteration-{iteration:03d}'
    smoke.EVIDENCE.mkdir(parents=True)
    try:
        # Reinstall the controller's immutable exact candidate before every independent probe.
        if sha256(root / f'{policy.app_id}.apk') != state['report']['identity']['apkSha256']:
            raise ValueError('Runtime candidate changed after preparation')
        module_apk = root / 'module/app-debug.apk'
        module_apk.parent.mkdir(parents=True, exist_ok=True)
        temporary = module_apk.with_suffix('.partial')
        try:
            shutil.copyfile(apk, temporary)
            temporary.replace(module_apk)
        finally:
            temporary.unlink(missing_ok=True)
        module_sha = sha256(module_apk)
        candidate = state['report']
        if not candidate.get('staticReport') or not candidate.get('staticReportSha256'):
            raise ValueError('Candidate lacks the shared static checker report identity')
        static_path = Path(candidate['staticReport']).resolve()
        if sha256(static_path) != candidate.get('staticReportSha256'):
            raise ValueError('Shared checker report changed after candidate preparation')
        static_report = json.loads(static_path.read_text())
        module_identity = prepare_module(apk, static_report, workspace)
        if module_identity['sha256'] != module_sha:
            raise ValueError('Module identity was prepared for different APK bytes')
        (root / 'module-identity.json').write_text(json.dumps(module_identity, indent=2) + '\n')
        regression_rows = run_regression(policy.app_id, root / 'regression')
        if any(row.get('status') != 'PASSED' for row in regression_rows):
            raise ValueError('Fixed acceptance regression failed; runtime acceptance is blocked')
        regression = {'status': 'PASSED', 'records': regression_rows}
        static = {'status': 'PASSED', 'identity': candidate['identity'],
                  'expectedPlan': candidate['expectedPlan'], 'inputHashes': static_report['inputHashes'],
                  'module': module_identity}
        smoke.adb('install', '-r', '-g', str(root / f'{policy.app_id}.apk'), timeout=240)
        result = smoke.adb('install', '-r', str(module_apk), timeout=120)
        (smoke.EVIDENCE / 'module-install.txt').write_bytes(result.stdout + result.stderr)
        candidate = dict(state['report'], appId=policy.app_id)
        report = probe_candidate(root, smoke.EVIDENCE, candidate)
        result = 0 if report['status'] == 'RUNTIME_SMOKE_VERIFIED' else 1
    except Exception:
        try:
            smoke.snapshot('failure')
            smoke.collect_logs()
        finally:
            evidence = workspace / 'work/analysis/runtime' / smoke.EVIDENCE.name
            shutil.copytree(smoke.EVIDENCE, evidence, dirs_exist_ok=True)
        raise
    evidence = workspace / 'work/analysis/runtime' / smoke.EVIDENCE.name
    shutil.copytree(smoke.EVIDENCE, evidence, dirs_exist_ok=True)
    report = json.loads((smoke.EVIDENCE / 'smoke-report.json').read_text())
    report.setdefault('moduleApkSha256', module_sha)
    (directory / 'runtime-report.json').write_text(json.dumps(report, indent=2) + '\n')
    if (report['moduleApkSha256'] != module_sha or sha256(module_apk) != module_sha
            or sha256(apk) != module_sha):
        raise ValueError('Runtime evidence does not match the verified module APK')
    candidate = state['report']
    for item in candidate['apkFiles']:
        if sha256(Path(item['path'])) != item['sha256']:
            raise ValueError('Candidate base or split APK changed after static verification')
    if sha256(Path(candidate['staticReport'])) != candidate['staticReportSha256']:
        raise ValueError('Shared checker report changed during runtime acceptance')
    verify_module_identity(module_identity, module_apk, workspace)
    validation = evaluate_validation(candidate, module_identity, static, regression, report,
                                     'success' if report.get('status') == 'RUNTIME_SMOKE_VERIFIED' else 'failure')
    (directory / 'validation-report.json').write_text(json.dumps(validation, indent=2) + '\n')
    if validation['outcome'] != 'VERIFIED':
        raise ValueError('Shared validation outcome ' + validation['outcome'] + ': '
                         + json.dumps(validation['diagnostics']))
    if result:
        diagnostic = report.get('runtimeDiagnostics', {})
        summary = {key: diagnostic.get(key, 'NOT_OBSERVED')
                   for key in ['moduleLoaded', 'compatibility', 'hookInstallation', 'resolvedHooks']}
        guidance = ''
        if diagnostic.get('hookInstallation') == f'INSTALLED_{len(policy.required_hook_ids)}':
            guidance = ' The controller observed installed hooks; missing module injection is not supported by this probe. Reassess the selected decision method, its callers, early returns and cached results.'
        raise ValueError('Independent ReDroid verification failed: ' + report.get('reason', report['status'])
                         + '. Controller runtime observations: ' + json.dumps(summary) + guidance
                         + '. Inspect screenshots, UI XML and hook logs in ' + str(evidence.relative_to(workspace)))
    if report.get('apkSha256') != state['report']['identity']['apkSha256']:
        raise ValueError('Runtime evidence does not match the candidate APK')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--serial', default=SERIAL)
    args = parser.parse_args()
    initialize(args.directory.resolve(), args.root.resolve(), args.serial)


if __name__ == '__main__':
    main()
