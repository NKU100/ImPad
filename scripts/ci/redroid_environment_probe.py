"""Test native hosted ReDroid and the pinned Magisk/Next/LSPosed framework."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import time
import urllib.request
import zipfile

BASE_IMAGE = 'redroid/redroid:14.0.0_64only-latest@sha256:0a611199ba2e0b5d60af39b3327a517f6407231f4352114ed3bd3cbfe2be69aa'
SERIAL = '127.0.0.1:16555'
CONTAINER = 'impad-hosted-redroid-probe'
MAGISK = '/debug_ramdisk/magisk'
ASSETS = {
    'magisk.apk': ('https://github.com/topjohnwu/Magisk/releases/download/v31.0/Magisk-v31.0.apk', '2c8a488b9a5293e578e95ae4f07e3c57aba4feec4a52ca4dd852a2692d6dd4e8'),
    'next.zip': ('https://github.com/LSPosed/ZygiskNext/releases/download/v1.5.2/Zygisk-Next-1.5.2-864-a4a72ce-release.zip', 'ce50b13fecb9e4456db69ef17b715f7cfbc094004dd9f30f7d6348dfab619a96'),
    'lsposed.zip': (None, 'ed37de2d2097aa6ba871377e7bf4ac719b708484f9468928e6abae10ab311e74'),
}


def checked_digest(path, expected):
    digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    if digest != expected:
        raise ValueError(f'Asset hash mismatch: {Path(path).name}')
    return digest


class Probe:
    def __init__(self, output):
        self.output = output.resolve()
        self.output.mkdir(parents=True, exist_ok=True)
        self.report = {'status': 'FAILED', 'stage': 'HOST', 'hostArchitecture': platform.machine(),
                       'baseImage': BASE_IMAGE, 'acceptanceKind': 'FRAMEWORK_ENVIRONMENT_ONLY'}

    def run(self, args, name, timeout=60, check=True):
        result = subprocess.run(args, capture_output=True, timeout=timeout)
        (self.output / (name + '.txt')).write_bytes(result.stdout + result.stderr)
        if check and result.returncode:
            raise RuntimeError(f'{name} exited {result.returncode}; see saved log')
        return result

    def adb(self, args, name, **kwargs):
        return self.run(['adb', '-s', SERIAL, *args], name, **kwargs)

    def shell(self, command, name, **kwargs):
        return self.adb(['shell', command], name, **kwargs)

    def boot(self, label):
        self.run(['adb', 'connect', SERIAL], label + '-connect', check=False)
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            result = self.shell('getprop sys.boot_completed', label + '-boot', check=False, timeout=10)
            if result.returncode == 0 and result.stdout.strip() == b'1':
                self.adb(['root'], label + '-adb-root')
                self.adb(['wait-for-device'], label + '-adb-ready', timeout=30)
                reconnect_deadline = min(deadline, time.monotonic() + 30)
                while time.monotonic() < reconnect_deadline:
                    self.run(['adb', 'connect', SERIAL], label + '-root-connect', check=False)
                    properties = self.shell('getprop; ps -A', label + '-properties', check=False, timeout=10)
                    if properties.returncode == 0:
                        return
                    time.sleep(1)
                raise RuntimeError(f'{label} root transport did not reconnect')
            time.sleep(3)
        raise RuntimeError(f'{label} boot timed out')

    def start(self, image, label):
        self.run(['sudo', 'docker', 'run', '-d', '--privileged', '--name', CONTAINER,
                  '-p', '127.0.0.1:16555:5555', '-v', str(self.output / 'data') + ':/data',
                  image, 'androidboot.use_memfd=true', 'androidboot.redroid_gpu_mode=guest',
                  'androidboot.redroid_width=720', 'androidboot.redroid_height=1280'], label + '-start')
        self.boot(label)

    def execute(self, app_inputs=None):
        try:
            if platform.machine() not in ('aarch64', 'arm64'):
                raise RuntimeError('Native ARM64 host required')
            self.run(['uname', '-a'], 'host-kernel')
            self.run(['getconf', 'PAGESIZE'], 'host-page-size')
            self.run(['free', '-k'], 'host-memory')
            self.run(['sudo', 'sh', '-c', 'cat /boot/config-$(uname -r); cat /proc/filesystems; ls /dev/*binder*; cat /sys/kernel/security/lsm'], 'host-kernel-config', check=False)
            modules = self.run(['sudo', 'modprobe', 'binder_linux', 'devices=binder,hwbinder,vndbinder'], 'binder-modprobe', check=False)
            binder_present = Path('/sys/module/binder_linux').exists()
            if modules.returncode and not binder_present:
                self.run(['sudo', 'sh', '-c', 'apt-get install -y linux-modules-extra-$(uname -r)'], 'binder-extra-modules', timeout=180, check=False)
                self.run(['sudo', 'modprobe', 'binder_linux', 'devices=binder,hwbinder,vndbinder'], 'binder-modprobe-retry')
            self.report['stage'] = 'STOCK_BOOT'
            self.start(BASE_IMAGE, 'stock')
            self.report['stockBoot'] = 'BOOT_COMPLETED'
            self.run(['sudo', 'docker', 'rm', '-f', CONTAINER], 'stock-remove')
            self.report['stage'] = 'FRAMEWORK_IMAGE'
            assets = self.output / 'assets'
            assets.mkdir()
            for name, (url, digest) in ASSETS.items():
                path = assets / name
                if url:
                    urllib.request.urlretrieve(url, path)
                else:
                    self.run(['gh', 'release', 'download', 'experimental-ramdisk-api37.2-r06-magisk31.0',
                              '--repo', 'NKU100/ImPad', '--pattern', 'LSPosed-v2.2.1-7912-release.zip', '--dir', str(assets)], 'download-lsposed')
                    (assets / 'LSPosed-v2.2.1-7912-release.zip').rename(path)
                checked_digest(path, digest)
            context = self.output / 'context'
            target = context / 'system/etc/impad-magisk'
            target.mkdir(parents=True)
            (context / 'debug_ramdisk').mkdir()
            with zipfile.ZipFile(assets / 'magisk.apk') as archive:
                for item in archive.namelist():
                    if item.startswith('assets/') and '/' not in item[7:]:
                        (target / item[7:]).write_bytes(archive.read(item))
                for item in archive.namelist():
                    if item.startswith('lib/arm64-v8a/lib') and item.endswith('.so'):
                        (target / Path(item).name[3:-3]).write_bytes(archive.read(item))
            templates = Path(__file__).parent / 'redroid'
            shutil.copyfile(templates / 'bootstrap.sh', target / 'bootstrap.sh')
            init = context / 'system/etc/init'
            init.mkdir()
            shutil.copyfile(templates / 'impad-magisk.rc', init / 'impad-magisk.rc')
            (context / 'Dockerfile').write_text(f'FROM {BASE_IMAGE}\nCOPY system/ /system/\nCOPY debug_ramdisk/ /debug_ramdisk/\n')
            self.run(['sudo', 'docker', 'build', '-t', 'impad-hosted-magisk31', str(context)], 'framework-build', timeout=300)
            self.start('impad-hosted-magisk31', 'magisk')
            root = self.shell(MAGISK + ' su -c id', 'magisk-root')
            if b'uid=0' not in root.stdout:
                raise RuntimeError('Magisk root not confirmed')
            version = self.shell(MAGISK + ' -V', 'magisk-version')
            if version.stdout.strip() != b'31000':
                raise RuntimeError('Magisk version mismatch')
            for name in ('next.zip', 'lsposed.zip'):
                self.adb(['push', str(assets / name), '/data/local/tmp/' + name], 'push-' + name)
                self.shell('export PATH=/debug_ramdisk:/data/adb/magisk:$PATH; magisk --install-module /data/local/tmp/' + name, 'install-' + name, timeout=180)
            self.shell(MAGISK + ' --sqlite "REPLACE INTO settings (key,value) VALUES (\'zygisk\',0)"', 'builtin-zygisk-disable')
            self.report['stage'] = 'FRAMEWORK_BOOT'
            self.run(['sudo', 'docker', 'restart', CONTAINER], 'framework-restart')
            self.boot('framework')
            state = self.shell('cat /data/adb/modules/zygisksu/module.prop; cat /data/adb/modules/zygisk_lsposed/module.prop; /data/adb/modules/zygisksu/bin/zygiskd status; ps -A; for p in $(pidof zygote64); do cat /proc/$p/maps; done', 'framework-state')
            if b'libzygisk.so' not in state.stdout or b'lspd' not in state.stdout:
                raise RuntimeError('Next injection or LSPosed daemon not observed')
            self.report['status'] = 'FRAMEWORK_ENVIRONMENT_READY'
            if app_inputs is not None:
                self.report.update(stage='APPLICATION_ACCEPTANCE', status='APPLICATION_ACCEPTANCE_FAILED',
                                   acceptanceKind='THREE_APP_RUNTIME_REGRESSION')
                from scripts.ci.redroid_app_smoke import AppSmoke
                AppSmoke(app_inputs, self).execute()
                self.report['status'] = 'APPLICATION_ACCEPTANCE_VERIFIED'
        except Exception as error:
            self.report['reason'] = f'{type(error).__name__}: {error}'
        finally:
            self.run(['sudo', 'docker', 'logs', CONTAINER], 'container-log', check=False)
            self.run(['sudo', 'docker', 'stats', '--no-stream', CONTAINER], 'container-resources', check=False)
            self.run(['sudo', 'dmesg', '-T'], 'host-dmesg', check=False)
            self.adb(['logcat', '-d'], 'android-logcat', check=False, timeout=15)
            self.run(['sudo', 'docker', 'rm', '-f', CONTAINER], 'cleanup', check=False)
            (self.output / 'report.json').write_text(json.dumps(self.report, indent=2) + '\n')
            (self.output / 'summary.md').write_text(f"ReDroid: **{self.report['status']}** at {self.report['stage']}.\n\n" + self.report.get('reason', '') + '\n\nFramework boot does not establish application hook or request acceptance.\n')
        return self.report['status'] == ('APPLICATION_ACCEPTANCE_VERIFIED' if app_inputs is not None else 'FRAMEWORK_ENVIRONMENT_READY')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--app-inputs', type=Path)
    args = parser.parse_args()
    raise SystemExit(0 if Probe(args.output).execute(args.app_inputs) else 1)


if __name__ == '__main__':
    main()
