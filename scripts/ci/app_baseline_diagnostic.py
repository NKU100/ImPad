"""Compare registered app startup with a stock or Magisk-only AVD."""
import json
import os
import time
from scripts.ci import runtime_smoke as smoke
from scripts.ci.qq_sigbus_diagnostic import prepare_layer


def outcome(logs, ready, package):
    if any('Fatal signal' in line and f'({package})' in line for line in logs.splitlines()):
        return 'APP_CRASHED'
    return 'BASELINE_VERIFIED' if ready else 'INCONCLUSIVE'


def main():
    smoke.configure_app_id(os.environ['APP_ID'])
    root = smoke.ROOT
    evidence = smoke.EVIDENCE
    evidence.mkdir(parents=True, exist_ok=True)
    layer = os.environ.get('QQ_DIAGNOSTIC_LAYER', 'stock')
    if layer not in {'stock', 'magisk', 'zygisk'}:
        raise ValueError('Baseline diagnosis requires stock, Magisk or built-in Zygisk')
    report = dict(status='INCONCLUSIVE', appId=smoke.APP_ID, environment=layer, iterations=[])
    try:
        prepare_layer(layer, runtime=smoke)
        if layer == 'stock':
            recipe = json.loads(open('environment/avd-api31-google.json').read())
            image = os.path.join(os.environ['ANDROID_HOME'], 'system-images/android-31/google_apis/x86_64/ramdisk.img')
            digest = smoke.apk_digest(image)
            if digest != recipe['systemImage']['stockRamdiskSha256']:
                raise RuntimeError('Stock ramdisk hash mismatch')
            report['stockRamdiskSha256'] = digest
            smoke.enable_page_size_backcompat = lambda: smoke.adb('shell', 'sh', '-c', smoke.PAGE_SIZE_BACKCOMPAT_COMMAND)
        packages = smoke.adb('shell', 'pm', 'list', 'packages')
        smoke.save('packages.txt', packages)
        forbidden = ['io.github.nku100.impad', 'org.lsposed.manager']
        if layer == 'stock':
            forbidden.append('com.topjohnwu.magisk')
        if any(p in packages.stdout.decode() for p in forbidden):
            raise RuntimeError('Unexpected framework or module package')
        apk = root / f'{smoke.APP_ID}.apk'
        report['apkSha256'] = smoke.apk_digest(apk)
        expected = os.environ.get('BASELINE_APK_SHA256')
        if not expected or report['apkSha256'] != expected:
            raise RuntimeError('Baseline APK differs from the requested comparison APK')
        smoke.save('app-install.txt', smoke.adb('install', '-r', '-g', str(apk), timeout=240))
        smoke.adb('push', str(root / 'ui-hierarchy.jar'), '/data/local/tmp/ui-hierarchy.jar')
        for iteration in range(1, 4):
            smoke.EVIDENCE = evidence / f'iteration-{iteration}'
            smoke.EVIDENCE.mkdir()
            smoke.adb('shell', 'am', 'force-stop', smoke.APP_PACKAGE)
            smoke.adb('shell', 'pm', 'clear', smoke.APP_PACKAGE)
            smoke.adb('logcat', '-c')
            ready, error = False, None
            try:
                smoke.mobile_input('baseline-mobile')
                ready = True
                time.sleep(10)
            except RuntimeError as exc:
                error = str(exc)
            logs = smoke.adb('logcat', '-d', '-v', 'threadtime')
            smoke.save('logcat.txt', logs)
            smoke.save('exit-info.txt', smoke.adb('shell', 'dumpsys', 'activity', 'exit-info', smoke.APP_PACKAGE))
            smoke.save('memory.txt', smoke.adb('shell', 'cat', '/proc/meminfo'))
            smoke.snapshot('final')
            report['iterations'].append(dict(iteration=iteration, loginInputVisible=ready, error=error,
                                             status=outcome(logs.stdout.decode(errors='replace'), ready, smoke.APP_PACKAGE)))
        statuses = [item['status'] for item in report['iterations']]
        report['status'] = ('APP_CRASHED' if 'APP_CRASHED' in statuses else
                            'BASELINE_VERIFIED' if all(s == 'BASELINE_VERIFIED' for s in statuses) else 'INCONCLUSIVE')
        smoke.adb('root', check=False)
        smoke.adb('wait-for-device')
        smoke.adb('pull', '/data/tombstones', str(evidence / 'tombstones'), check=False)
    except Exception as exc:
        report['error'] = str(exc)
    finally:
        smoke.EVIDENCE = evidence
        (evidence / 'diagnostic-report.json').write_text(json.dumps(report, indent=2) + '\n')
        (evidence / 'summary.md').write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps(report))
    return 0 if report['status'] == 'BASELINE_VERIFIED' else 1


if __name__ == '__main__':
    raise SystemExit(main())
