"""Boot an accelerated stock API 31 AVD and observe three pinned official apps."""
import json
import os
from pathlib import Path
import platform
import shutil
import shlex
import subprocess
import time
from scripts.ci import runtime_smoke as smoke
from scripts.ci.app_policy import get_policy
from scripts.ci.fetch_latest_wechat_apk import download_candidate

APPS = {
    'qq': ('9.3.70', 'https://downv6.qq.com/qqweb/QQ_1/android_apk/9.3.70.41925_2a98ecf56e5ef04b.apk', 'e8c28713b740c18246989d47916d9fbbad06d440778a5aab3f4e108a2423e55c'),
    'wechat': ('8.0.79', 'https://dldir1v6.qq.com/weixin/android/weixin8079android3200_0x28004f30_arm64_1.apk', 'f2a725bc04302ad218a563533ae79356e66b64aa711b275bc919a8d432f7e15d'),
    'wecom': ('5.0.12', 'https://dldir1.qq.com/wework/work_weixin/WeCom_android_5.0.12.83835_arm64_100001.apk', '6b3cead315fa7d8c302b4b538f25abe5afc935b69f9cd49ee212a780f6afdf40'),
}


def app_outcome(records, ready):
    if any(r['reason'] in (2, 3, 4, 5, 6) for r in records):
        return 'APP_FAILED'
    return 'LOGIN_UI_VERIFIED' if ready else 'INCONCLUSIVE'


def exit_records(text, package):
    records = []
    import re
    for block in re.split(r'ApplicationExitInfo #\d+:', text)[1:]:
        match = re.search(r'process=([^\s]+).*?reason=(\d+).*?status=(\d+)', block, re.S)
        if match and (match[1] == package or match[1].startswith(package + ':')):
            records.append(dict(process=match[1], reason=int(match[2]), status=int(match[3])))
    return records


def main():
    root = Path('work/runner-probe').resolve()
    evidence = root / 'stock-avd'
    evidence.mkdir(parents=True, exist_ok=True)
    host_report = json.loads((root / 'report.json').read_text())
    report = dict(status='SETUP_FAILED', apps=[], avdBoot='NOT_COMPLETED', api=31,
                  imageAbi='arm64-v8a' if platform.machine().lower() in {'arm64', 'aarch64'} else 'x86_64')
    process = None
    sdk = Path(os.environ['ANDROID_HOME'])
    suffix = '.bat' if os.name == 'nt' else ''
    sdkmanager = sdk / ('cmdline-tools/latest/bin/sdkmanager' + suffix)
    avdmanager = sdk / ('cmdline-tools/latest/bin/avdmanager' + suffix)
    adb = sdk / 'platform-tools' / ('adb.exe' if os.name == 'nt' else 'adb')
    def run(args, **kwargs):
        if str(args[0]).endswith('.bat'):
            kwargs['shell'] = True
        return subprocess.run(list(map(str, args)), **kwargs)
    def device(*args, timeout=60, check=True):
        if args[0] == 'shell':
            args = ('shell', ' '.join(shlex.quote(str(arg)) for arg in args[1:]))
        result = run([adb, '-s', 'emulator-5580', *args], capture_output=True, timeout=timeout)
        if check and result.returncode:
            raise RuntimeError((result.stdout + result.stderr).decode(errors='replace'))
        return result
    try:
        image = f"system-images;android-31;google_apis;{report['imageAbi']}"
        run([sdkmanager, 'platform-tools', 'platforms;android-37.2', 'build-tools;37.0.0', image], check=True, timeout=600)
        image_root = sdk / 'system-images/android-31/google_apis' / report['imageAbi']
        report['ramdiskSha256'] = smoke.apk_digest(image_root / 'ramdisk.img')
        shutil.copy2(image_root / 'source.properties', evidence / 'image-source.properties')
        run([avdmanager, 'create', 'avd', '-n', 'ImPadStockProbe', '-k', image, '-d', 'pixel_5', '--force'], input=b'no\n', check=True, timeout=120)
        emulator = root / 'sdk/emulator' / ('emulator.exe' if os.name == 'nt' else 'emulator')
        started = time.monotonic()
        with (evidence / 'emulator.log').open('wb') as log:
            process = subprocess.Popen([str(emulator), '@ImPadStockProbe', '-port', '5580', '-accel', 'on',
                '-memory', '4096', '-cores', '4', '-no-window', '-no-snapshot', '-noaudio', '-no-boot-anim',
                '-gpu', 'swiftshader_indirect', '-verbose'], stdout=log, stderr=subprocess.STDOUT)
        while time.monotonic() - started < 600:
            if process.poll() is not None:
                raise RuntimeError(f'Emulator exited with code {process.returncode}')
            value = device('shell', 'getprop', 'sys.boot_completed', check=False, timeout=15)
            if value.stdout.strip() == b'1':
                break
            time.sleep(5)
        else:
            raise RuntimeError('AVD boot did not complete within 600 seconds')
        report.update(avdBoot='BOOT_COMPLETED', bootSeconds=round(time.monotonic() - started, 1))
        device('root', check=False)
        device('wait-for-device')
        if b'uid=0' not in device('shell', 'id').stdout:
            raise RuntimeError('Stock userdebug root unavailable')
        for path in ('/debug_ramdisk/magisk', '/sbin/magisk', '/data/adb/lspd', '/data/adb/modules'):
            if device('shell', 'test', '-e', path, check=False).returncode == 0:
                raise RuntimeError('Unexpected root framework path: ' + path)
        packages = device('shell', 'pm', 'list', 'packages')
        (evidence / 'packages-before.txt').write_bytes(packages.stdout)
        if any(package in packages.stdout.decode() for package in ('com.topjohnwu.magisk', 'org.lsposed.manager', 'io.github.nku100.impad')):
            raise RuntimeError('Unexpected framework or module package')
        (evidence / 'properties.txt').write_bytes(device('shell', 'getprop').stdout)
        (evidence / 'boot.png').write_bytes(device('exec-out', 'screencap', '-p').stdout)
        classes = root / 'ui-classes'
        classes.mkdir(exist_ok=True)
        run(['javac', '--release', '17', '-cp', sdk / 'platforms/android-37.2/android.jar', '-d', classes,
             'scripts/ci/UiHierarchy.java'], check=True, timeout=120)
        jar = root / 'ui-hierarchy.jar'
        run([sdk / ('build-tools/37.0.0/d8' + suffix), '--min-api', '28', '--lib', sdk / 'platforms/android-37.2/android.jar',
             '--output', jar, classes / 'UiHierarchy.class'], check=True, timeout=120)
        device('push', str(jar), '/data/local/tmp/ui-hierarchy.jar')
        smoke.adb = device
        smoke.enable_page_size_backcompat = lambda: device('shell', 'sh', '-c', smoke.PAGE_SIZE_BACKCOMPAT_COMMAND)
        for app, (version, url, digest) in APPS.items():
            smoke.configure_app_id(app)
            smoke.EVIDENCE = evidence / app
            smoke.EVIDENCE.mkdir()
            result = dict(appId=app, version=version, apkSha256=digest, status='SETUP_FAILED')
            report['apps'].append(result)
            try:
                apk, downloaded, _, _ = download_candidate(url, root / 'apk-cache' / app, digest,
                    {'content_length': None, 'last_modified': '', 'etag': ''}, get_policy(app))
                if downloaded != digest:
                    raise RuntimeError('APK checksum mismatch')
                signer = run([sdk / ('build-tools/37.0.0/apksigner' + suffix), 'verify', '--print-certs', apk], capture_output=True, check=True, timeout=120)
                smoke.save('signer.txt', signer)
                if get_policy(app).signer_sha256 not in signer.stdout.decode():
                    raise RuntimeError('APK signer mismatch')
                smoke.save('install.txt', device('install', '-r', '-g', str(apk), timeout=240))
                device('shell', 'pm', 'clear', smoke.APP_PACKAGE)
                device('logcat', '-c')
                ready = False
                try:
                    smoke.mobile_input('login')
                    ready = True
                    time.sleep(30)
                except RuntimeError as error:
                    result['navigationError'] = str(error)
                exits = device('shell', 'dumpsys', 'activity', 'exit-info', smoke.APP_PACKAGE)
                records = exit_records(exits.stdout.decode(errors='replace'), smoke.APP_PACKAGE)
                smoke.save('exit-info.txt', exits)
                smoke.save('logcat.txt', device('logcat', '-d', '-v', 'threadtime'))
                smoke.save('memory.txt', device('shell', 'cat', '/proc/meminfo'))
                smoke.save('maps.txt', device('shell', 'sh', '-c', 'for p in $(pidof ' + smoke.APP_PACKAGE + ' ' + smoke.APP_PACKAGE + ':MSF); do echo PID=$p; cat /proc/$p/maps; done', check=False))
                smoke.snapshot('final')
                result.update(status=app_outcome(records, ready), loginInputVisible=ready, exits=records)
            except Exception as error:
                result['error'] = str(error)
            finally:
                device('shell', 'am', 'force-stop', smoke.APP_PACKAGE, check=False)
                (evidence / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
        device('pull', '/data/tombstones', str(evidence / 'tombstones'), check=False)
        report['status'] = 'OBSERVATION_COMPLETE'
    except Exception as error:
        report['error'] = str(error)
    finally:
        if process is not None:
            try:
                device('emu', 'kill', check=False, timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
        (evidence / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
        host_report['avdBoot'] = report['avdBoot']
        (root / 'report.json').write_text(json.dumps(host_report, indent=2) + '\n')
        summary = f"\nStock AVD: {report['avdBoot']}\n\n| App | Version | Result |\n|---|---|---|\n"
        for app in report['apps']:
            summary += f"| {app['appId']} | {app['version']} | {app['status']} |\n"
        if report.get('error'):
            summary += '\n' + report['error'] + '\n'
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as output:
            output.write(summary)
        print(json.dumps(report))
    return 0 if report['status'] == 'OBSERVATION_COMPLETE' else 1


if __name__ == '__main__':
    raise SystemExit(main())
