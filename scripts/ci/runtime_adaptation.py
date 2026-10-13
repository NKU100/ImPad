"""Attach a disposable AVD and independently validate every adaptation turn."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess

from scripts.ci import runtime_smoke as smoke
from scripts.ci.app_policy import get_policy
from scripts.ci.codex_adaptation import sha256


def configure(directory):
    state = json.loads((directory / 'state.json').read_text())
    app_id = state.get('appId', state.get('report', {}).get('appId'))
    policy = get_policy(app_id)
    smoke.configure_app_id(policy.app_id)
    root = Path(state['runtime']['root'])
    smoke.ROOT = root
    socket = Path(state['runtime']['socket'])
    os.environ['ADB_SERVER_SOCKET'] = 'localfilesystem:' + str(socket)
    os.environ['ANDROID_SERIAL'] = state['runtime']['serial']
    return state, root


def initialize(directory, root, serial):
    devices = subprocess.check_output(['adb', 'devices'], text=True).splitlines()[1:]
    connected = [line.split()[0] for line in devices if line.strip()]
    if connected != [serial] or not serial.startswith('emulator-'):
        raise RuntimeError('Runtime adaptation requires exactly one disposable emulator')
    state_path = directory / 'state.json'
    state = json.loads(state_path.read_text())
    policy = get_policy(state.get('appId', state.get('report', {}).get('appId')))
    smoke.configure_app_id(policy.app_id)
    workspace = Path(state['worktree'])
    candidate_apk = root / f'{policy.app_id}.apk'
    if sha256(candidate_apk) != state['report']['identity']['apkSha256']:
        raise ValueError('Runtime candidate does not match verified identity')
    if policy.app_id == 'qq':
        from scripts.ci.apps.qq.prepare_probe import prepare
        device_abi = smoke.adb('shell', 'getprop', 'ro.product.cpu.abi').stdout.decode().strip()
        probe_abi = {'x86_64': 'x86_64', 'arm64-v8a': 'arm64'}.get(device_abi)
        if probe_abi is None:
            raise ValueError('QQ independent observer requires a supported emulator ABI')
        prepare(root, probe_abi)
        (root / 'candidate-report.json').write_text(json.dumps(state['report'], indent=2))
    smoke.ROOT = root
    smoke.EVIDENCE = root / 'prepare-evidence'
    os.environ['ANDROID_SERIAL'] = serial
    if smoke.main(mode='prepare'):
        raise RuntimeError('AVD preparation failed before model invocation; see preparation evidence')
    socket = workspace / 'work/analysis/adb.sock'
    # The controller has already verified root, baseline UI and official LSPosed.
    # Move the dedicated ADB server to a filesystem socket visible inside the model sandbox.
    subprocess.run(['adb', 'kill-server'], check=True)
    address = 'localfilesystem:' + str(socket)
    subprocess.run(['adb', '-L', address, 'start-server'], check=True)
    env = dict(os.environ, ADB_SERVER_SOCKET=address, ANDROID_SERIAL=serial)
    subprocess.run(['adb', 'wait-for-device'], env=env, check=True, timeout=120)
    devices = subprocess.check_output(['adb', 'devices'], env=env, text=True).splitlines()[1:]
    connected = [line.split()[0] for line in devices if line.strip()]
    if connected != [serial]:
        raise RuntimeError('The model ADB server must expose exactly the dedicated emulator')
    state['runtime'] = {'root': str(root), 'serial': serial, 'socket': str(socket)}
    state_path.write_text(json.dumps(state, indent=2))
    runtime_instructions = workspace / 'work/analysis/runtime-device.json'
    runtime_instructions.parent.mkdir(parents=True, exist_ok=True)
    runtime_instructions.write_text(json.dumps({
        'appId': policy.app_id, 'serial': serial, 'baseline': policy.runtime_baseline,
        'instructions': f'Run adb app-launch to restore the managed runtime configuration and start {policy.package_name}. After adding the profile, run impad-build for guarded host checks/tests/build; do not retry Gradle inside the network-disabled sandbox. Build logs are under work/analysis/host-build. Use adb device commands to inspect the disposable AVD. Interrupted requests are cancelled by the relay. Independent runtime verification runs after every completed turn.'}, indent=2))


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
        smoke.adb('install', '-r', '-g', str(root / f'{policy.app_id}.apk'), timeout=240)
        result = smoke.adb('install', '-r', str(apk), timeout=120)
        (smoke.EVIDENCE / 'module-install.txt').write_bytes(result.stdout + result.stderr)
        smoke.reboot()
        smoke.configure_manager()
        result = smoke.main(mode='probe')
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
    report['moduleApkSha256'] = sha256(apk)
    (directory / 'runtime-report.json').write_text(json.dumps(report, indent=2) + '\n')
    if result:
        diagnostic = report.get('runtimeDiagnostics', {})
        summary = {key: diagnostic.get(key, 'NOT_OBSERVED')
                   for key in ['moduleLoaded', 'compatibility', 'hookInstallation', 'resolvedHooks']}
        guidance = ''
        if diagnostic.get('hookInstallation') == f'INSTALLED_{len(policy.required_hook_ids)}':
            guidance = ' The controller observed installed hooks; missing module injection is not supported by this probe. Reassess the selected decision method, its callers, early returns and cached results.'
        raise ValueError('Independent AVD verification failed: ' + report.get('reason', report['status'])
                         + '. Controller runtime observations: ' + json.dumps(summary) + guidance
                         + '. Inspect screenshots, UI XML and hook logs in ' + str(evidence.relative_to(workspace)))
    if report.get('apkSha256') != state['report']['identity']['apkSha256']:
        raise ValueError('Runtime evidence does not match the candidate APK')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--serial', default='emulator-5554')
    args = parser.parse_args()
    initialize(args.directory.resolve(), args.root.resolve(), args.serial)


if __name__ == '__main__':
    main()
