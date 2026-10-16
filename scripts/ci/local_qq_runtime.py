"""Run the shared QQ acceptance flow on an explicitly selected local AVD."""
import argparse
import json
import os
from pathlib import Path
import shutil


def image_configuration(version):
    if version not in {'30', '37.2'}:
        raise ValueError('Unsupported Android version')
    tag = 'google_apis' if version == '30' else 'google_apis_ps16k'
    suffix = version.replace('.', '_')
    return dict(package=f'system-images;android-{version};{tag};arm64-v8a',
                avd='ImPadQqApi' + suffix,
                output='work/qq-local-api' + suffix)


def validate_device(version, sdk, abi, emulator):
    if emulator != '1':
        raise ValueError('Local QQ acceptance requires an emulator')
    if sdk != version.split('.')[0]:
        raise ValueError('Selected AVD has the wrong Android API')
    if abi != 'arm64-v8a':
        raise ValueError('Local Mac acceptance requires the ARM64 image')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', choices=['30', '37.2'], required=True)
    parser.add_argument('--serial', required=True)
    parser.add_argument('--inputs', type=Path, required=True)
    args = parser.parse_args()
    if not args.serial.startswith('emulator-'):
        parser.error('Select a dedicated emulator serial')
    os.environ.update(ANDROID_SERIAL=args.serial, QQ_DIAGNOSTIC_LAYER='zygisk-next-lsposed', QQ_NEXT_LINKER='system')
    from scripts.ci import runtime_smoke as smoke
    properties = [smoke.adb('shell', 'getprop', key).stdout.decode().strip()
                  for key in ('ro.build.version.sdk', 'ro.product.cpu.abi', 'ro.kernel.qemu')]
    validate_device(args.version, *properties)
    root = Path(image_configuration(args.version)['output'])
    root.mkdir(parents=True, exist_ok=True)
    smoke.ROOT, smoke.EVIDENCE = root, root / 'evidence'
    smoke.EVIDENCE.mkdir(exist_ok=True)
    if (smoke.EVIDENCE / 'smoke-report.json').exists():
        raise ValueError('Preserve or move the previous evidence before starting another run')
    for name in ('qq.apk', 'magisk.apk', 'zygisk-next.zip', 'lsposed.zip', 'manager.apk', 'ui-hierarchy.jar', 'module/app-debug.apk'):
        destination = root / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(args.inputs / name, destination)
    for candidate in ('/debug_ramdisk/magisk', '/sbin/magisk'):
        if smoke.adb('shell', 'test', '-x', candidate, check=False).returncode == 0:
            smoke.MAGISK = candidate
            break
    else:
        raise RuntimeError('Launch the AVD with its verified Magisk ramdisk first')
    smoke.adb('root')
    smoke.adb('wait-for-device')
    smoke.adb('shell', smoke.MAGISK, '--sqlite',
              'REPLACE INTO policies (uid,policy,until,logging,notification) VALUES (2000,2,0,1,0)')
    smoke.adb('unroot')
    smoke.adb('wait-for-device')
    smoke.save('device-properties.txt', smoke.adb('shell', 'getprop'))
    smoke.save('page-size.txt', smoke.adb('shell', 'getconf', 'PAGESIZE'))
    from scripts.ci.apps.qq.prepare_probe import prepare
    prepare(root, 'arm64')
    smoke.main(app_id='qq')
    report = json.loads((smoke.EVIDENCE / 'smoke-report.json').read_text())
    if report['status'] != 'RUNTIME_SMOKE_VERIFIED':
        raise SystemExit(1)


if __name__ == '__main__':
    main()
