"""Compare QQ MSF native crashes across isolated framework layers."""
import json
import os
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


def layer_contract(layer):
    layers = {'stock': (False, False, False), 'magisk': (True, False, False),
              'zygisk': (True, True, False), 'zygisk-next': (True, False, False),
              'lsposed': (True, True, True)}
    if layer not in layers:
        raise ValueError('Unknown diagnostic layer')
    return layers[layer]


def prepare_layer(layer):
    magisk, zygisk, lsposed = layer_contract(layer)
    if not magisk:
        smoke.save('adb-root.txt', smoke.adb('root'))
        smoke.adb('wait-for-device')
        if 'uid=0' not in smoke.adb('shell', 'id').stdout.decode():
            raise RuntimeError('Stock image did not provide diagnostic root')
        for path in ('/debug_ramdisk/magisk', '/data/adb/lspd', '/data/adb/modules/zygisk_lsposed'):
            if smoke.adb('shell', 'test', '-e', path, check=False).returncode == 0:
                raise RuntimeError('Stock environment contains ' + path)
        return
    smoke.bootstrap_magisk()
    setting = 'REPLACE INTO settings (key,value) VALUES (\'zygisk\',' + str(int(zygisk)) + ')'
    smoke.save('zygisk-setting.txt', smoke.su(smoke.MAGISK + ' --sqlite "' + setting + '"'))
    if layer == 'zygisk-next':
        package = smoke.ROOT / 'zygisk-next.zip'
        if smoke.apk_digest(package) != 'ce50b13fecb9e4456db69ef17b715f7cfbc094004dd9f30f7d6348dfab619a96':
            raise RuntimeError('Zygisk Next package mismatch')
        smoke.adb('push', str(package), '/data/local/tmp/zygisk-next.zip')
        smoke.save('zygisk-next-install.txt', smoke.su('export PATH=/debug_ramdisk:/data/adb/magisk:$PATH; magisk --install-module /data/local/tmp/zygisk-next.zip', timeout=240))
    if lsposed:
        smoke.adb('push', str(smoke.ROOT / 'lsposed.zip'), '/data/local/tmp/lsposed.zip')
        smoke.save('lsposed-install.txt', smoke.su('export PATH=/debug_ramdisk:/data/adb/magisk:$PATH; magisk --install-module /data/local/tmp/lsposed.zip', timeout=240))
        smoke.adb('install', '-r', str(smoke.ROOT / 'manager.apk'))
    smoke.reboot()
    if 'uid=0' not in smoke.su('id').stdout.decode():
        raise RuntimeError('Magisk root unavailable after reboot')
    if layer == 'zygisk-next':
        setting = smoke.su(smoke.MAGISK + ' --sqlite "SELECT value FROM settings WHERE key=\'zygisk\'"')
        smoke.save('builtin-zygisk-after-reboot.txt', setting)
        if setting.stdout.strip() != b'value=0':
            raise RuntimeError('Built-in Zygisk is not disabled')
        for _ in range(45):
            maps = smoke.su('for p in $(pidof zygote64); do cat /proc/$p/maps; done')
            if b'/data/adb/modules/zygisksu/lib64/libzygisk.so' in maps.stdout:
                smoke.save('zygote-maps.txt', maps)
                break
            time.sleep(2)
        else:
            raise RuntimeError('Zygisk Next library absent from zygote64')
        smoke.save('zygisk-next-state.txt', smoke.su('cat /data/adb/modules/zygisksu/module.prop; cat /data/adb/zygisksu/znctx; ps -A'))
    if lsposed:
        for _ in range(45):
            if smoke.su('pidof lspd', check=False).stdout.strip():
                break
            time.sleep(2)
        else:
            raise RuntimeError('LSPosed daemon unavailable')
    elif smoke.su('test -e /data/adb/modules/zygisk_lsposed', check=False).returncode == 0:
        raise RuntimeError('Unexpected LSPosed installation')
    smoke.save('layer-processes.txt', smoke.su('ps -A; ' + smoke.MAGISK + ' --sqlite "SELECT * FROM settings"'))


def main():
    smoke.configure_app_id('qq')
    root = smoke.ROOT
    smoke.EVIDENCE.mkdir(parents=True, exist_ok=True)
    layer = os.environ.get('QQ_DIAGNOSTIC_LAYER', 'stock')
    report = dict(status='INCONCLUSIVE', environment=layer, iterations=[], events=[])
    def event(phase, **details):
        report['events'].append(dict(time=datetime.now(timezone.utc).isoformat(), phase=phase, **details))
        (smoke.EVIDENCE / 'diagnostic-report.json').write_text(json.dumps(report, indent=2) + '\n')
    try:
        event('ENVIRONMENT_CHECK')
        smoke.save('device-properties.txt', smoke.adb('shell', 'getprop'))
        prepare_layer(layer)
        smoke.save('shell-identity.txt', smoke.adb('shell', 'id'))
        packages = smoke.adb('shell', 'pm', 'list', 'packages').stdout.decode()
        smoke.save('packages.txt', smoke.adb('shell', 'pm', 'list', 'packages'))
        forbidden = ['io.github.nku100.impad']
        if layer == 'stock':
            forbidden += ['org.lsposed.manager', 'com.topjohnwu.magisk']
        if any(p in packages for p in forbidden):
            raise RuntimeError('Unexpected framework/module package')
        if layer == 'stock':
            smoke.adb('shell', 'setprop', 'bionic.linker.16kb.app_compat.enabled', 'true')
            smoke.adb('shell', 'setprop', 'pm.16kb.app_compat.disabled', 'false')
        else:
            smoke.enable_page_size_backcompat()
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
                        if layer == 'zygisk-next':
                            smoke.save('msf-maps.txt', smoke.su('for p in $(pidof com.tencent.mobileqq:MSF); do echo PID=$p; cat /proc/$p/maps; done'))
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
        if layer != 'stock':
            smoke.adb('root', check=False)
            smoke.adb('wait-for-device')
        smoke.adb('pull', '/data/tombstones', str(root / 'evidence/tombstones'), check=False)
    except Exception as error:
        report['error'] = str(error)
        smoke.save('failure-logcat.txt', smoke.adb('logcat', '-d', '-v', 'threadtime', check=False))
    finally:
        smoke.EVIDENCE = root / 'evidence'
        event('COMPLETE')
        (smoke.EVIDENCE / 'summary.md').write_text('# QQ framework crash diagnosis\n\n' + json.dumps(report, indent=2) + '\n')
        print(json.dumps(report))
    return 0 if report['status'] == 'NOT_REPRODUCED' else 1


if __name__ == '__main__':
    raise SystemExit(main())
