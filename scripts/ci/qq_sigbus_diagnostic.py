"""Compare QQ MSF native crashes across isolated framework layers."""
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from scripts.ci import runtime_smoke as smoke


THIRD_PARTY_MODULES = {
    'tablethook': dict(package='com.roro.tablethook', label='TabletHook',
                      url='https://github.com/roro2239/TabletHook/releases/download/1-1.0/TabletHook-v1.0.apk',
                      sha256='4b9d70ecb278dd4474abdddf621d77de5800fbf146a51ee064787e6c55cb6686',
                      marker='QQ MSF 平板设备类型已启用'),
    'qqtablet': dict(package='top.hookvip.wxtablet.qq', label='QQTablet',
                     url='https://github.com/hswwls/WeChatTablet/releases/download/WeChatTablet/QQTablet-1.0.5.apk',
                     sha256='5b9f4e06afe00afa2deab99ca7e574b1f8de8e04bcb05a1f4c12489b11ac2fd0',
                     marker='forced AppSetting and PadUtil to TABLET in MSF process'),
}


def download_third_party():
    import urllib.request
    selected = os.environ['QQ_DIAGNOSTIC_MODULE']
    module = THIRD_PARTY_MODULES[selected]
    path = smoke.ROOT / 'third-party.apk'
    with urllib.request.urlopen(module['url'], timeout=120) as response:
        path.write_bytes(response.read())
    if smoke.apk_digest(path) != module['sha256']:
        raise RuntimeError('Third-party module download mismatch')
    (smoke.EVIDENCE / 'third-party-package.json').write_text(json.dumps(module, indent=2) + '\n')


def prepare_third_party(selected):
    from scripts.ci.apps.qq.runtime_probe import installed_hash, manager_state
    module = THIRD_PARTY_MODULES[selected]
    path = smoke.ROOT / 'third-party.apk'
    if smoke.apk_digest(path) != module['sha256']:
        raise RuntimeError('Third-party module package mismatch')
    smoke.MODULE = module['package']
    smoke.MODULE_LABEL = module['label']
    smoke.save('third-party-install.txt', smoke.adb('install', '-r', str(path)))
    if installed_hash(smoke, smoke.MODULE) != module['sha256']:
        raise RuntimeError('Installed third-party module mismatch')
    smoke.configure_manager()
    state = manager_state(smoke)
    if state['moduleEnabled'] is not True:
        raise RuntimeError('Third-party module is not enabled')
    smoke.reboot()
    state = manager_state(smoke)
    smoke.save('third-party-state.txt', smoke.su('ps -A'))
    return dict(module, **state)


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
              'zygisk-next-lsposed': (True, False, True), 'lsposed': (True, True, True)}
    if layer not in layers:
        raise ValueError('Unknown diagnostic layer')
    return layers[layer]


def prepare_layer(layer, runtime=smoke):
    magisk, zygisk, lsposed = layer_contract(layer)
    if not magisk:
        runtime.save('adb-root.txt', runtime.adb('root'))
        runtime.adb('wait-for-device')
        if 'uid=0' not in runtime.adb('shell', 'id').stdout.decode():
            raise RuntimeError('Stock image did not provide diagnostic root')
        for path in ('/debug_ramdisk/magisk', '/data/adb/lspd', '/data/adb/modules/zygisk_lsposed'):
            if runtime.adb('shell', 'test', '-e', path, check=False).returncode == 0:
                raise RuntimeError('Stock environment contains ' + path)
        return
    runtime.bootstrap_magisk()
    setting = 'REPLACE INTO settings (key,value) VALUES (\'zygisk\',' + str(int(zygisk)) + ')'
    runtime.save('zygisk-setting.txt', runtime.su(runtime.MAGISK + ' --sqlite "' + setting + '"'))
    if layer in {'zygisk-next', 'zygisk-next-lsposed'}:
        package = runtime.ROOT / 'zygisk-next.zip'
        if runtime.apk_digest(package) != 'ce50b13fecb9e4456db69ef17b715f7cfbc094004dd9f30f7d6348dfab619a96':
            raise RuntimeError('Zygisk Next package mismatch')
        runtime.adb('push', str(package), '/data/local/tmp/zygisk-next.zip')
        runtime.save('zygisk-next-install.txt', runtime.su('export PATH=/debug_ramdisk:/data/adb/magisk:$PATH; magisk --install-module /data/local/tmp/zygisk-next.zip', timeout=240))
    if lsposed:
        runtime.adb('push', str(runtime.ROOT / 'lsposed.zip'), '/data/local/tmp/lsposed.zip')
        runtime.save('lsposed-install.txt', runtime.su('export PATH=/debug_ramdisk:/data/adb/magisk:$PATH; magisk --install-module /data/local/tmp/lsposed.zip', timeout=240))
        runtime.adb('install', '-r', str(runtime.ROOT / 'manager.apk'))
    runtime.reboot()
    if 'uid=0' not in runtime.su('id').stdout.decode():
        raise RuntimeError('Magisk root unavailable after reboot')
    if layer in {'zygisk-next', 'zygisk-next-lsposed'}:
        setting = runtime.su(runtime.MAGISK + ' --sqlite "SELECT value FROM settings WHERE key=\'zygisk\'"')
        runtime.save('builtin-zygisk-after-reboot.txt', setting)
        if setting.stdout.strip() != b'value=0':
            raise RuntimeError('Built-in Zygisk is not disabled')
        for _ in range(45):
            maps = runtime.su('for p in $(pidof zygote64); do cat /proc/$p/maps; done')
            if b'/data/adb/modules/zygisksu/lib64/libzygisk.so' in maps.stdout:
                runtime.save('zygote-maps.txt', maps)
                break
            time.sleep(2)
        else:
            raise RuntimeError('Zygisk Next library absent from zygote64')
        runtime.save('zygisk-next-state.txt', runtime.su('cat /data/adb/modules/zygisksu/module.prop; cat /data/adb/zygisksu/znctx; ps -A'))
    if lsposed:
        for _ in range(45):
            if runtime.su('pidof lspd', check=False).stdout.strip():
                break
            time.sleep(2)
        else:
            raise RuntimeError('LSPosed daemon unavailable')
    elif runtime.su('test -e /data/adb/modules/zygisk_lsposed', check=False).returncode == 0:
        raise RuntimeError('Unexpected LSPosed installation')
    runtime.save('layer-processes.txt', runtime.su('ps -A; ' + runtime.MAGISK + ' --sqlite "SELECT * FROM settings"'))


def main():
    smoke.configure_app_id('qq')
    root = smoke.ROOT
    smoke.EVIDENCE.mkdir(parents=True, exist_ok=True)
    layer = os.environ.get('QQ_DIAGNOSTIC_LAYER', 'stock')
    selected_module = os.environ.get('QQ_DIAGNOSTIC_MODULE', 'none')
    if selected_module != 'none' and layer != 'zygisk-next-lsposed':
        raise ValueError('Third-party diagnosis requires Zygisk Next and LSPosed')
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
        if selected_module != 'none':
            report['thirdPartyModule'] = prepare_third_party(selected_module)
            event('THIRD_PARTY_ENABLED')
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
            smoke.save('memory-before.txt', smoke.adb('shell', 'cat', '/proc/meminfo'))
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
                        if layer in {'zygisk-next', 'zygisk-next-lsposed'}:
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
            smoke.save('memory-after.txt', smoke.adb('shell', 'cat', '/proc/meminfo'))
            smoke.save('exit-info.txt', exits)
            smoke.save('logcat.txt', smoke.adb('logcat', '-d', '-v', 'threadtime'))
            framework = smoke.su('for d in /data/adb/lspd/log /data/adb/lspd/logs; do if [ -d "$d" ]; then find "$d" -type f -exec cat {} +; fi; done', check=False)
            smoke.save('lsposed-logs.txt', framework)
            smoke.snapshot('final')
            records = [r for r in msf_exits(exits.stdout.decode()) if (r['pid'], r['timestamp']) not in seen]
            all_records.extend(records)
            completed = completed and ready
            report['iterations'].append(dict(iteration=iteration, loginInputVisible=ready, exits=records,
                                             status=outcome(records, ready)))
            event('ITERATION_COMPLETE', iteration=iteration)
        report['status'] = outcome(all_records, completed)
        if selected_module != 'none':
            marker = THIRD_PARTY_MODULES[selected_module]['marker']
            logs = '\n'.join(p.read_text(errors='replace') for p in (root / 'evidence').glob('iteration-*/*logs.txt'))
            logs += '\n'.join(p.read_text(errors='replace') for p in (root / 'evidence').glob('iteration-*/logcat.txt'))
            report['thirdPartyModule']['hookObserved'] = marker in logs
            if not report['thirdPartyModule']['hookObserved'] and report['status'] == 'NOT_REPRODUCED':
                report['status'] = 'INCONCLUSIVE'
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
