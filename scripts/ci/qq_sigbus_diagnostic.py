"""Reproduce QQ MSF native crashes on a stock, unmodified hosted AVD."""
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from scripts.ci import runtime_smoke as smoke


def msf_exits(text):
    records = []
    for block in re.split(r'ApplicationExitInfo #\d+:', text)[1:]:
        match = re.search(r'timestamp=([^\n]+?) pid=(\d+).*?process=com\.tencent\.mobileqq:MSF reason=(\d+) \([^\n]*?\).*?status=(\d+)', block, re.S)
        if match:
            records.append(dict(timestamp=match[1], pid=int(match[2]), reason=int(match[3]), signal=int(match[4])))
    return records


def outcome(records, completed):
    if any(r['reason'] == 2 and r['signal'] == 7 for r in records):
        return 'SIGBUS_REPRODUCED'
    if not completed:
        return 'INCONCLUSIVE'
    if any(r['reason'] not in (10, 13) for r in records):
        return 'OTHER_PROCESS_FAILURE'
    return 'NOT_REPRODUCED'


def main():
    smoke.configure_app_id('qq')
    root = smoke.ROOT
    smoke.EVIDENCE.mkdir(parents=True, exist_ok=True)
    report = dict(status='INCONCLUSIVE', environment='STOCK_NO_MODULE', iterations=[], events=[])
    def event(phase, **details):
        report['events'].append(dict(time=datetime.now(timezone.utc).isoformat(), phase=phase, **details))
        (smoke.EVIDENCE / 'diagnostic-report.json').write_text(json.dumps(report, indent=2) + '\n')
    try:
        event('ENVIRONMENT_CHECK')
        smoke.save('device-properties.txt', smoke.adb('shell', 'getprop'))
        smoke.save('adb-root.txt', smoke.adb('root'))
        smoke.adb('wait-for-device')
        smoke.save('shell-identity.txt', smoke.adb('shell', 'id'))
        if 'uid=0' not in smoke.adb('shell', 'id').stdout.decode():
            raise RuntimeError('Stock image did not provide diagnostic root')
        for path in ('/debug_ramdisk/magisk', '/data/adb/lspd', '/data/adb/modules/zygisk_lsposed'):
            result = smoke.adb('shell', 'test', '-e', path, check=False)
            if result.returncode == 0:
                raise RuntimeError('Stock environment contains ' + path)
        packages = smoke.adb('shell', 'pm', 'list', 'packages').stdout.decode()
        smoke.save('packages.txt', smoke.adb('shell', 'pm', 'list', 'packages'))
        if any(p in packages for p in ('io.github.nku100.impad', 'org.lsposed.manager', 'com.topjohnwu.magisk')):
            raise RuntimeError('Stock environment contains framework/module packages')
        smoke.adb('shell', 'setprop', 'bionic.linker.16kb.app_compat.enabled', 'true')
        smoke.adb('shell', 'setprop', 'pm.16kb.app_compat.disabled', 'false')
        smoke.adb('push', str(root / 'ui-hierarchy.jar'), '/data/local/tmp/ui-hierarchy.jar')
        smoke.save('qq-install.txt', smoke.adb('install', '-r', str(root / 'qq.apk'), timeout=120))
        from scripts.ci.apps.qq.runtime_probe import installed_hash
        report['apkSha256'] = smoke.apk_digest(root / 'qq.apk')
        if installed_hash(smoke, smoke.APP_PACKAGE) != report['apkSha256']:
            raise RuntimeError('Installed QQ APK mismatch')
        all_records = []
        completed = True
        for iteration in range(1, 4):
            smoke.EVIDENCE = root / 'evidence' / f'iteration-{iteration}'
            smoke.EVIDENCE.mkdir(parents=True)
            smoke.adb('shell', 'am', 'force-stop', smoke.APP_PACKAGE)
            before = msf_exits(smoke.adb('shell', 'dumpsys', 'activity', 'exit-info', smoke.APP_PACKAGE).stdout.decode())
            seen = {(r['pid'], r['timestamp']) for r in before}
            smoke.adb('shell', 'pm', 'clear', smoke.APP_PACKAGE)
            smoke.adb('logcat', '-c')
            event('QQ_LAUNCH', iteration=iteration)
            smoke.adb('shell', 'am', 'start', '-n', smoke.UI_STRATEGY.launcher_component)
            deadline = time.monotonic() + 90
            ready = False
            while time.monotonic() < deadline:
                activity, xml = smoke.snapshot('current')
                if smoke.UI_STRATEGY.login_input_ready(activity, xml, smoke.APP_PACKAGE):
                    if not ready:
                        event('LOGIN_INPUT_VISIBLE', iteration=iteration)
                        ready = True
                else:
                    button = smoke.unique_node(xml, lambda n: n.get('package') == smoke.APP_PACKAGE
                                              and n.get('clickable') == 'true' and n.get('enabled') != 'false'
                                              and smoke.UI_STRATEGY.is_login_navigation_label(n.get('text', '')))
                    if button is not None:
                        event('NAVIGATE', iteration=iteration, label=button.get('text'))
                        smoke.tap(button, accessibility=True)
                # Deny only the system's log access dialog; no login is submitted.
                if 'Allow QQ to access all device logs?' in xml:
                    deny = smoke.unique_node(xml, lambda n: n.get('package') == 'android' and n.get('text') in {'Don’t allow', "Don't allow"})
                    if deny is not None:
                        event('DENY_LOG_ACCESS', iteration=iteration)
                        smoke.tap(deny)
                time.sleep(2)
            exits = smoke.adb('shell', 'dumpsys', 'activity', 'exit-info', smoke.APP_PACKAGE)
            smoke.save('exit-info.txt', exits)
            smoke.save('logcat.txt', smoke.adb('logcat', '-d', '-v', 'threadtime'))
            smoke.snapshot('final')
            records = [r for r in msf_exits(exits.stdout.decode()) if (r['pid'], r['timestamp']) not in seen]
            all_records.extend(records)
            completed = completed and ready
            report['iterations'].append(dict(iteration=iteration, loginInputVisible=ready, exits=records,
                                             status=outcome(records, ready)))
            event('ITERATION_COMPLETE', iteration=iteration)
        report['status'] = outcome(all_records, completed)
        smoke.adb('pull', '/data/tombstones', str(root / 'evidence/tombstones'), check=False)
    except Exception as error:
        report['error'] = str(error)
        smoke.save('failure-logcat.txt', smoke.adb('logcat', '-d', '-v', 'threadtime', check=False))
    finally:
        smoke.EVIDENCE = root / 'evidence'
        event('COMPLETE')
        (smoke.EVIDENCE / 'summary.md').write_text('# QQ stock AVD crash diagnosis\n\n' + json.dumps(report, indent=2) + '\n')
        print(json.dumps(report))
    return 0 if report['status'] == 'NOT_REPRODUCED' else 1


if __name__ == '__main__':
    raise SystemExit(main())
