#!/usr/bin/env python3
"""Verify local APK identities and parse their code on an explicit, isolated emulator."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import time
import zipfile

import run_probe as common


def validate_cases(manifest):
    if not isinstance(manifest, dict) or manifest.get('schemaVersion') != 1:
        raise ValueError('Unsupported input matrix schema')
    cases = manifest.get('cases')
    if not isinstance(cases, list) or not cases:
        raise ValueError('Input matrix needs at least one case')
    names = set()
    for case in cases:
        name = case.get('name') if isinstance(case, dict) else None
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', name):
            raise ValueError(f'Invalid case name: {name!r}')
        if name in names:
            raise ValueError(f'Duplicate case name: {name}')
        names.add(name)
    for case in cases:
        if case.get('appId') not in {'wechat', 'qq', 'wecom'}:
            raise ValueError('Unsupported app identity')
        inputs = case.get('codeInputs')
        if not isinstance(inputs, list) or not inputs:
            raise ValueError('Each case needs codeInputs')
        for source in inputs:
            if not isinstance(source, dict) or not Path(source.get('path', '')).is_file():
                raise ValueError('A local code input is missing')
            if not re.fullmatch(r'[a-fA-F0-9]{64}', source.get('sha256', '')):
                raise ValueError('Every code input needs a pinned content hash')
        indices = case.get('packageApkIndices')
        if not isinstance(indices, list) or not indices or any(type(i) is not int or not 0 <= i < len(inputs) for i in indices):
            raise ValueError('Each case needs valid base/split packageApkIndices')
        expected = case.get('expectedPackage')
        if not isinstance(expected, dict) or not {'packageName', 'versionCode', 'versionName', 'signerSha256'} <= expected.keys():
            raise ValueError('Each package needs a pinned expected identity')
    return cases


def verified_signer(output):
    signers = {match.lower() for match in re.findall(
        r'(?m)^(?:Signer #\d+|V\d+(?:\.\d+)? Signer(?: #\d+)?)[ :]+certificate SHA-256 digest: ([a-fA-F0-9]{64})\s*$', output)}
    if len(signers) != 1:
        raise ValueError('Missing or multiple verified package signers')
    return signers.pop()


def parse_badging(output):
    line = next((line for line in output.splitlines() if line.startswith('package:')), None)
    if line is None:
        raise ValueError('Missing package manifest identity')
    fields = dict(re.findall(r"(?:^|\s)(name|versionCode|versionCodeMajor|versionName)='([^']*)'", line))
    return {'packageName': fields['name'], 'versionCode': (int(fields.get('versionCodeMajor', '0')) << 32) |
            (int(fields['versionCode']) & 0xffffffff), 'versionName': fields.get('versionName', '')}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--serial', required=True)
    p.add_argument('--cases', required=True, type=Path)
    p.add_argument('--output', required=True, type=Path)
    p.add_argument('--sdk', type=Path, default=Path.home() / 'Library/Android/sdk')
    p.add_argument('--module-apk', type=Path, default=common.ROOT / 'app/build/outputs/apk/debug/app-debug.apk')
    args = p.parse_args(argv)
    common.validate_serial(args.serial)
    cases = validate_cases(json.loads(args.cases.read_text()))
    output = common.create_output(args.output.resolve())
    if not re.fullmatch(r'[A-Za-z0-9._-]+', output.name):
        raise ValueError('Output basename must be a simple run ID')
    (output / 'input-matrix.json').write_text(json.dumps(cases, indent=2) + '\n')
    commands = []
    env = dict(os.environ)
    java = subprocess.check_output(['/usr/libexec/java_home', '-v', '21'], text=True).strip()
    env['JAVA_HOME'] = java

    def run(cmd, timeout=300):
        started = time.monotonic()
        result = subprocess.run([str(c) for c in cmd], capture_output=True, text=True, env=env, timeout=timeout)
        commands.append({'argv': [str(c) for c in cmd], 'exitCode': result.returncode,
                         'elapsedSeconds': time.monotonic() - started, 'stdout': result.stdout, 'stderr': result.stderr})
        (output / 'commands.json').write_text(json.dumps(commands, indent=2) + '\n')
        if result.returncode:
            raise RuntimeError(f'Command failed: {cmd}: {result.stderr[-1800:]} {result.stdout[-1800:]}')
        return result.stdout.strip()

    sdk = args.sdk.resolve()
    tools = sdk / 'build-tools/37.0.0'
    lock = json.loads((common.ROOT / 'gradle/dexkit-lock.json').read_text())
    jar_hash = common.verify_hash(common.ROOT / 'build/dexkit/android-arm64-v8a/dexkit.jar', lock['artifacts']['jvmApi']['sha256'])
    native_hash = common.verify_hash(common.ROOT / 'build/dexkit/android-arm64-v8a/libdexkit.so', lock['artifacts']['androidArm64V8a']['sha256'])
    frozen_module = output / 'module.apk'
    shutil.copyfile(args.module_apk, frozen_module)
    module_sha256 = hashlib.sha256(frozen_module.read_bytes()).hexdigest()
    with zipfile.ZipFile(frozen_module) as archive:
        if hashlib.sha256(archive.read('lib/arm64-v8a/libdexkit.so')).hexdigest() != native_hash:
            raise ValueError('Module native identity differs from lock')
    identities = []
    for case in cases:
        for source in case['codeInputs']:
            common.verify_hash(Path(source['path']), source['sha256'].lower())
        package_identities = []
        arm64 = False
        for index in case['packageApkIndices']:
            path = Path(case['codeInputs'][index]['path'])
            badging = run([tools / 'aapt2', 'dump', 'badging', path])
            cert = run([tools / 'apksigner', 'verify', '--print-certs', path])
            actual = dict(parse_badging(badging), signerSha256=verified_signer(cert))
            if package_identities and not actual['versionName']:
                actual['versionName'] = package_identities[0]['versionName']
            if actual != case['expectedPackage']:
                raise ValueError(f'Package identity differs from pinned expectation: {case["name"]}: {actual}')
            package_identities.append(actual)
            with zipfile.ZipFile(path) as archive:
                arm64 = arm64 or any(n.startswith('lib/arm64-v8a/') for n in archive.namelist())
        if not arm64:
            raise ValueError('Package inputs do not contain the required arm64 ABI')
        identities.append(package_identities[0])

    adb = sdk / 'platform-tools/adb'
    def device(*parts):
        return run([adb, '-s', args.serial, *parts])
    if device('shell', 'getprop', 'ro.kernel.qemu') != '1' or device('shell', 'getprop', 'sys.boot_completed') != '1':
        raise ValueError('Target is not a booted emulator')
    classes = output / 'classes'; classes.mkdir()
    dex = output / 'dex'; dex.mkdir()
    android = sdk / 'platforms/android-37.2/android.jar'
    frozen_probe_source = output / 'SemanticInputProbe.java'
    shutil.copyfile(common.SOURCE / 'SemanticInputProbe.java', frozen_probe_source)
    run([Path(java) / 'bin/javac', '-source', '8', '-target', '8', '-classpath', android,
         '-d', classes, frozen_probe_source])
    run([tools / 'd8', '--min-api', '26', '--lib', android, '--output', dex, *classes.rglob('*.class')])
    remote = '/data/local/tmp/impad-semantic-' + output.name
    device('shell', 'mkdir', remote)
    device('push', dex / 'classes.dex', remote + '/probe.dex')
    device('push', frozen_module, remote + '/module.apk')
    if device('shell', 'sha256sum', remote + '/module.apk').split()[0] != module_sha256:
        raise ValueError('Uploaded module hash differs from frozen artifact')
    summary = {'schemaVersion': 1, 'scope': 'REAL_DEX_PARSE_REFLECTION_PROBE', 'runId': output.name,
               'moduleSha256': module_sha256,
               'probeSha256': hashlib.sha256((dex / 'classes.dex').read_bytes()).hexdigest(),
               'dexkitJarSha256': jar_hash, 'dexkitNativeSha256': native_hash, 'reports': []}
    for case, identity in zip(cases, identities):
        directory = remote + '/' + case['name']
        device('shell', 'mkdir', directory)
        names = []
        for index, source in enumerate(case['codeInputs']):
            name = f'code-{index}.apk'
            device('push', source['path'], directory + '/' + name)
            if device('shell', 'sha256sum', directory + '/' + name).split()[0] != source['sha256'].lower():
                raise ValueError('Uploaded code input differs from pinned hash')
            names.append(name)
        device_input = dict(identity, caseName=case['name'], appId=case['appId'], codeFiles=names,
                            apkSha256=case['codeInputs'][case['packageApkIndices'][0]]['sha256'])
        input_path = output / (case['name'] + '-input.json')
        input_path.write_text(json.dumps(device_input, indent=2) + '\n')
        device('push', input_path, directory + '/input.json')
        command = ' '.join(['CLASSPATH=' + shlex.quote(remote + '/probe.dex'), 'app_process', '/',
                            'SemanticInputProbe', shlex.quote(directory), shlex.quote(remote + '/module.apk')])
        device('shell', command)
        report_path = output / (case['name'] + '-report.json')
        device('pull', directory + '/report.json', report_path)
        report = json.loads(report_path.read_text())
        summary['reports'].append(report)
        (output / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
        print(case['name'], report['status'], flush=True)
        device('shell', 'rm', '-rf', directory)
    (output / 'logcat.txt').write_text(device('logcat', '-d', '-t', '3000'))
    print(output / 'summary.json')
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, KeyError, OSError, RuntimeError, subprocess.TimeoutExpired) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
