#!/usr/bin/env python3
"""Build and run the isolated experiment on an explicitly named emulator."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time
import zipfile

ROOT = Path(__file__).resolve().parents[3]
SOURCE = Path(__file__).parent


def validate_serial(serial):
    if not re.fullmatch(r'emulator-[0-9]+', serial or ''):
        raise ValueError('an explicit emulator-N serial is required; physical devices are prohibited')


def create_output(path):
    path.mkdir(parents=True, exist_ok=False)
    return path


def verify_hash(path, expected):
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != expected:
        raise ValueError(f'hash mismatch: {path}: {actual} != {expected}')
    return actual


def write_archive(path, dexes):
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_STORED) as archive:
        for i, dex in enumerate(dexes, 1):
            archive.writestr('classes.dex' if i == 1 else f'classes{i}.dex', dex)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--serial', required=True)
    parser.add_argument('--entry-point', choices=['EffectiveCodeProbe', 'StartupSnapshotProbe'], default='EffectiveCodeProbe')
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--sdk', type=Path, default=Path.home() / 'Library/Android/sdk')
    parser.add_argument('--module-apk', type=Path, default=ROOT / 'app/build/outputs/apk/debug/app-debug.apk')
    args = parser.parse_args(argv)
    validate_serial(args.serial)
    output = create_output(args.output.resolve())
    lock = json.loads((ROOT / 'gradle/dexkit-lock.json').read_text())
    native_dir = ROOT / 'build/dexkit/android-arm64-v8a'
    jar = native_dir / 'dexkit.jar'
    native = native_dir / 'libdexkit.so'
    jar_hash = verify_hash(jar, lock['artifacts']['jvmApi']['sha256'])
    native_hash = verify_hash(native, lock['artifacts']['androidArm64V8a']['sha256'])
    with zipfile.ZipFile(args.module_apk) as module:
        if hashlib.sha256(module.read('lib/arm64-v8a/libdexkit.so')).hexdigest() != native_hash:
            raise ValueError('module native library differs from lock')
    sdk = args.sdk.resolve()
    java_home = subprocess.check_output(['/usr/libexec/java_home', '-v', '21'], text=True).strip()
    env = dict(os.environ, JAVA_HOME=java_home)
    adb = str(sdk / 'platform-tools/adb')
    commands = []

    def run(cmd, timeout=90):
        started = time.monotonic()
        p = subprocess.run([str(c) for c in cmd], capture_output=True, text=True, env=env, timeout=timeout)
        commands.append({'argv': [str(c) for c in cmd], 'exitCode': p.returncode,
                         'elapsedSeconds': time.monotonic() - started, 'stdout': p.stdout, 'stderr': p.stderr})
        (output / 'commands.json').write_text(json.dumps(commands, indent=2) + '\n')
        if p.returncode:
            raise RuntimeError(f'command failed: {cmd}: {p.stderr[-1500:]} {p.stdout[-1500:]}')
        return p.stdout.strip()

    def device(*cmd):
        return run([adb, '-s', args.serial, *cmd])

    if device('shell', 'getprop', 'ro.kernel.qemu') != '1':
        raise ValueError('target is not an emulator')
    if device('shell', 'getprop', 'sys.boot_completed') != '1':
        raise ValueError('emulator is not booted')
    android_jar = sdk / 'platforms/android-37.2/android.jar'
    d8 = sdk / 'build-tools/37.0.0/d8'
    for variant in ['a', 'b', 'same', 'extra']:
        classes = output / variant / 'classes'
        dex = output / variant / 'dex'
        classes.mkdir(parents=True); dex.mkdir()
        sources = sorted((SOURCE / 'fixtures' / variant).rglob('*.java'))
        run([Path(java_home) / 'bin/javac', '--release', '8', '-d', classes, *sources])
        run([d8, '--min-api', '26', '--output', dex, *sorted(classes.rglob('*.class'))])
        write_archive(output / f'{variant}.jar', [(dex / 'classes.dex').read_bytes()])
    write_archive(output / 'multi.jar', [(output / 'a/dex/classes.dex').read_bytes(), (output / 'extra/dex/classes.dex').read_bytes()])
    classes = output / 'probe-classes'; classes.mkdir()
    dex = output / 'probe-dex'; dex.mkdir()
    run([Path(java_home) / 'bin/javac', '-source', '8', '-target', '8', '-classpath', android_jar,
         '-d', classes, SOURCE / (args.entry_point + '.java')])
    run([d8, '--min-api', '26', '--lib', android_jar, '--output', dex, *sorted(classes.rglob('*.class'))])
    probe_hash = hashlib.sha256((dex / 'classes.dex').read_bytes()).hexdigest()
    run_id = output.name
    if not re.fullmatch(r'[A-Za-z0-9._-]+', run_id):
        raise ValueError('output basename must be a simple run identifier')
    remote = '/data/local/tmp/impad-effective-' + run_id
    device('shell', 'mkdir', remote)
    for item in ['a.jar', 'b.jar', 'same.jar', 'extra.jar', 'multi.jar']:
        device('push', str(output / item), remote + '/' + item)
    device('push', str(SOURCE / 'fixtures.json'), remote + '/fixtures.json')
    device('push', str(dex / 'classes.dex'), remote + '/probe.dex')
    device('push', str(args.module_apk.resolve()), remote + '/module.apk')
    shell_command = ' '.join(['CLASSPATH=' + shlex.quote(remote + '/probe.dex'), 'app_process', '/',
                              args.entry_point, shlex.quote(remote), shlex.quote(remote + '/module.apk')])
    device('shell', shell_command)
    device('pull', remote, str(output / 'device'))
    report = json.loads((output / 'device/device-report.json').read_text())
    report.update(schemaVersion=1, runId=run_id,
                  device={'serial': args.serial, 'api': report.pop('api'), 'abi': report.pop('abi')},
                  dependencies={'probeSha256': probe_hash, 'dexkitJarSha256': jar_hash, 'dexkitNativeSha256': native_hash})
    report['moduleSha256'] = hashlib.sha256(args.module_apk.read_bytes()).hexdigest()
    report['fixtureSourceSha256'] = {str(p.relative_to(SOURCE)): hashlib.sha256(p.read_bytes()).hexdigest()
                                     for p in SOURCE.rglob('*') if p.is_file() and '__pycache__' not in str(p)}
    (output / 'probe.json').write_text(json.dumps(report, indent=2) + '\n')
    (output / 'logcat.txt').write_text(device('logcat', '-d', '-t', '3000'))
    print(output / 'probe.json')
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
