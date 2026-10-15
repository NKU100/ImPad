"""Verify official LSPosed and WeChat login UI on a disposable hosted AVD."""

import hashlib
import base64
import json
import importlib
import csv
import argparse
import io
from pathlib import Path
import re
import shlex
import subprocess
import sqlite3
import tempfile
import sys
import time
import xml.etree.ElementTree as ET
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.ci.app_policy import get_policy

ROOT = Path('work/runtime-smoke')
EVIDENCE = ROOT / 'evidence'
MAGISK = '/debug_ramdisk/magisk'
PAGE_SIZE_BACKCOMPAT_COMMAND = 'setprop bionic.linker.16kb.app_compat.enabled true; setprop pm.16kb.app_compat.disabled false'
APP_ID = 'wechat'
APP_POLICY = get_policy(APP_ID)
APP_PACKAGE = APP_POLICY.package_name
UI_STRATEGY = importlib.import_module(APP_POLICY.runtime_ui_strategy)
MODULE = 'io.github.nku100.impad'
MODULE_LABEL = "I'm Pad"
stage = 'START'


def configure_app_id(app_id):
    global APP_ID, APP_POLICY, APP_PACKAGE, UI_STRATEGY
    APP_POLICY = get_policy(app_id)
    APP_ID = APP_POLICY.app_id
    APP_PACKAGE = APP_POLICY.package_name
    UI_STRATEGY = importlib.import_module(APP_POLICY.runtime_ui_strategy)


def run(*args, timeout=60, check=True):
    try:
        result = subprocess.run(args, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        if check:
            raise RuntimeError(f'{args[0]} timed out after {timeout} seconds') from error
        return subprocess.CompletedProcess(args, 124, error.stdout or b'', b'Command timed out')
    if check and result.returncode:
        raise RuntimeError(f'{args[0]} exited {result.returncode}: ' + (result.stdout + result.stderr).decode(errors='replace'))
    return result


def adb(*args, timeout=60, check=True):
    if args[0] == 'shell':
        args = ('shell', ' '.join(shlex.quote(arg) for arg in args[1:]))
    return run('adb', *args, timeout=timeout, check=check)


def su(command, **kwargs):
    return adb('shell', MAGISK, 'su', '-c', command, **kwargs)


def save(name, result):
    (EVIDENCE / name).write_bytes(result.stdout + result.stderr)


def nodes(xml):
    try:
        return list(ET.fromstring(xml).iter('node'))
    except ET.ParseError:
        return []


def has_app_ui(xml):
    return UI_STRATEGY.has_app_ui(xml, APP_PACKAGE)


has_wechat_ui = has_app_ui


def is_tablet_entry(text):
    return UI_STRATEGY.tablet_entry_visible(text)


def qr_page_ready(activity, xml):
    return UI_STRATEGY.qr_page_ready(activity, xml, APP_PACKAGE, APP_POLICY.runtime_qr_page)


def unique_node(xml, predicate):
    matches = [node for node in nodes(xml) if node.get('visible') != 'false' and predicate(node)]
    if len(matches) > 1:
        raise ValueError(f'UI selector is ambiguous ({len(matches)} matches)')
    return matches[0] if matches else None


def ocr_target(tsv, label):
    lines = {}
    for word in csv.DictReader(io.StringIO(tsv), delimiter='\t'):
        if word['level'] != '5' or not word['text'].strip():
            continue
        key = tuple(word[field] for field in ('page_num', 'block_num', 'par_num', 'line_num'))
        lines.setdefault(key, []).append(word)
    matches = []
    for words in lines.values():
        text = ' '.join(word['text'] for word in words)
        normalized_text = re.sub(r'[^\w]+', ' ', text.casefold()).strip()
        normalized_label = re.sub(r'[^\w]+', ' ', label.casefold()).strip()
        if normalized_text != normalized_label or any(float(word['conf']) < 70 for word in words):
            continue
        left = min(int(word['left']) for word in words)
        top = min(int(word['top']) for word in words)
        right = max(int(word['left']) + int(word['width']) for word in words)
        bottom = max(int(word['top']) + int(word['height']) for word in words)
        matches.append(ET.Element('node', {'text': text, 'bounds': f'[{left},{top}][{right},{bottom}]'}))
    if len(matches) > 1:
        raise ValueError(f'OCR selector is ambiguous ({len(matches)} matches)')
    return matches[0] if matches else None


def manager_ocr():
    result = run('tesseract', str(EVIDENCE / 'manager-current.png'), 'stdout', '--psm', '11', 'tsv')
    save('manager-current-ocr.tsv', result)
    return result.stdout.decode(errors='replace')


def snapshot(label):
    adb('shell', 'rm', '-f', '/data/local/tmp/hosted-smoke.xml')
    dump = adb('shell', 'env', 'CLASSPATH=/data/local/tmp/ui-hierarchy.jar',
               'app_process', '/system/bin', 'UiHierarchy', '/data/local/tmp/hosted-smoke.xml',
               timeout=25, check=False)
    save(label + '-dump.txt', dump)
    ui = adb('shell', 'cat', '/data/local/tmp/hosted-smoke.xml', check=False)
    save(label + '.xml', ui)
    screen = adb('exec-out', 'screencap', '-p', check=False)
    if screen.returncode == 0:
        (EVIDENCE / (label + '.png')).write_bytes(screen.stdout)
    activity = adb('shell', 'dumpsys', 'activity', 'activities')
    save(label + '-activity.txt', activity)
    return activity.stdout.decode(errors='replace'), ui.stdout.decode(errors='replace')


def tap(node, accessibility=False):
    bounds = [int(part) for part in re.findall(r'\d+', node.get('bounds', ''))]
    if len(bounds) != 4 or bounds[2] <= bounds[0] or bounds[3] <= bounds[1]:
        raise RuntimeError('Selected UI element has no visible bounds')
    if accessibility:
        label = node.get('text', '').strip()
        if (APP_ID != 'qq' or node.get('package') != APP_PACKAGE
                or node.get('enabled') == 'false' or node.get('clickable') != 'true'
                or not UI_STRATEGY.is_login_navigation_label(label)):
            raise ValueError('Accessibility navigation requires a QQ login navigation button')
        result = adb('shell', 'env', 'CLASSPATH=/data/local/tmp/ui-hierarchy.jar',
                     'app_process', '/system/bin', 'UiHierarchy',
                     '/data/local/tmp/login-click.xml', 'click', APP_PACKAGE, label,
                     timeout=25, check=False)
        save('login-click.txt', result)
        if result.returncode:
            return False
    else:
        adb('shell', 'input', 'tap', str((bounds[0] + bounds[2]) // 2), str((bounds[1] + bounds[3]) // 2))
    time.sleep(2)
    return True


def enable_page_size_backcompat():
    # Recovery libraries unpacked at runtime can still have 4 KB ELF alignment.
    su(PAGE_SIZE_BACKCOMPAT_COMMAND)


def mobile_input(label):
    enable_page_size_backcompat()
    adb('shell', 'am', 'force-stop', APP_PACKAGE)
    launcher_component = getattr(UI_STRATEGY, 'launcher_component', None)
    if launcher_component:
        adb('shell', 'am', 'start', '-n', launcher_component)
    else:
        adb('shell', 'monkey', '-p', APP_PACKAGE, '-c', 'android.intent.category.LAUNCHER', '1')
    ready = 0
    for attempt in range(30):
        activity, ui = snapshot(label + '-current')
        if UI_STRATEGY.login_input_ready(activity, ui, APP_PACKAGE):
            ready += 1
            if ready >= 2:
                snapshot(label)
                return ui
        else:
            ready = 0
            node = unique_node(ui, lambda n: UI_STRATEGY.is_login_navigation_label(n.get('text', '')))
            if node is not None:
                tap(node, accessibility=APP_ID == 'qq')
        time.sleep(2)
    for service in ('input', 'window', 'display', 'power'):
        save(label + '-' + service + '.txt', adb('shell', 'dumpsys', service, timeout=25, check=False))
    raise RuntimeError(f'{APP_ID} did not reach a stable, rendered login input UI')


def reboot():
    adb('reboot')
    adb('wait-for-device', timeout=180)
    for _ in range(90):
        if adb('shell', 'getprop', 'sys.boot_completed', check=False).stdout.strip() == b'1':
            user = adb('shell', 'am', 'get-started-user-state', '0', check=False)
            save('reboot-user-state.txt', user)
            if user.returncode == 0 and user.stdout.strip() == b'RUNNING_UNLOCKED':
                return
        time.sleep(2)
    raise RuntimeError('Device did not complete reboot and unlock user 0')


def bootstrap_magisk():
    files = ROOT / 'magisk-files'
    files.mkdir(exist_ok=True)
    device_abi = adb('shell', 'getprop', 'ro.product.cpu.abi').stdout.decode().strip()
    if device_abi not in {'x86_64', 'arm64-v8a'}:
        raise ValueError('Runtime Magisk bootstrap requires a supported emulator ABI')
    with zipfile.ZipFile(ROOT / 'magisk.apk') as apk:
        for name in apk.namelist():
            if name.startswith('assets/') and name.endswith('.sh'):
                (files / Path(name).name).write_bytes(apk.read(name))
        for binary in ('busybox', 'magisk', 'magiskboot', 'magiskinit', 'magiskpolicy', 'bootctl'):
            (files / binary).write_bytes(apk.read(f'lib/{device_abi}/lib{binary}.so'))
    adb('shell', 'mkdir', '-p', '/data/local/tmp/impad-magisk-files')
    for file in files.iterdir():
        adb('push', str(file), '/data/local/tmp/impad-magisk-files/' + file.name)
    save('magisk-bootstrap.txt', su('mkdir -p /data/adb/magisk; cp /data/local/tmp/impad-magisk-files/* /data/adb/magisk/; chmod -R 755 /data/adb/magisk'))
    save('preinit-device.txt', su(MAGISK + ' --preinit-device', check=False))
    adb('install', '-r', str(ROOT / 'magisk.apk'), timeout=120)


def module_enabled_from_database(path):
    connection = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)
    try:
        rows = connection.execute('SELECT enabled FROM modules_state WHERE module_pkg_name=? AND user_id=?', (MODULE, 0)).fetchall()
    finally:
        connection.close()
    if not rows:
        return False
    if len(rows) != 1 or rows[0][0] not in (0,1):
        raise ValueError('Unexpected LSPosed module enabled state')
    return bool(rows[0][0])


def manager_module_enabled():
    # Compose Manager exposes no switch state through the hierarchy dump.
    # Read a local database snapshot; all changes still use the official UI.
    state_path = EVIDENCE / 'manager-module-state.json'
    def unavailable(reason):
        state_path.write_text(json.dumps({'module':MODULE, 'enabled':None, 'reason':reason}) + '\n')
        return None
    with tempfile.TemporaryDirectory(prefix='impad-manager-state-') as temporary:
        db = Path(temporary) / 'modules_config.db'
        for suffix in ('', '-wal'):
            result = su('base64 /data/adb/lspd/config/modules_config.db' + suffix, check=False)
            if result.returncode:
                if not suffix:
                    return unavailable(result.stderr.decode(errors='replace')[-1000:])
                continue
            try:
                payload = base64.b64decode(b''.join(result.stdout.split()), validate=True)
            except ValueError:
                return unavailable('Invalid base64 database transport')
            Path(str(db) + suffix).write_bytes(payload)
        try:
            value = module_enabled_from_database(db)
        except (sqlite3.Error, ValueError) as error:
            return unavailable(str(error))
        state_path.write_text(json.dumps({'module':MODULE, 'enabled':value}) + '\n')
        return value


def configure_manager(enabled_state=True):
    adb('shell', 'am', 'start', '-n', 'org.lsposed.manager/.ui.activity.MainActivity')
    for _ in range(10):
        state = manager_module_enabled()
        if state is enabled_state:
            return
        _, ui = snapshot('manager-current')
        enabled = unique_node(ui, lambda n: 'Switch' in n.get('class', '') and n.get('checkable') == 'true')
        if enabled is not None and any(n.get('text') == MODULE_LABEL for n in nodes(ui)):
            if enabled.get('checked') != str(enabled_state).lower():
                tap(enabled)
                _, ui = snapshot('manager-enabled')
                enabled = unique_node(ui, lambda n: 'Switch' in n.get('class', '') and n.get('checkable') == 'true')
            if enabled is None or enabled.get('checked') != str(enabled_state).lower():
                raise RuntimeError(f'Manager did not confirm {MODULE_LABEL} enabled={enabled_state}')
            return
        tsv = manager_ocr()
        enable = ocr_target(tsv, 'Enable module')
        if enable is not None:
            if state is None:
                time.sleep(2)
                continue
            tap(enable)
            snapshot('manager-enable-requested')
            for _ in range(3):
                if manager_module_enabled() is enabled_state:
                    return
                time.sleep(1)
            raise RuntimeError('Manager action did not persist the requested enabled state')
        node = unique_node(ui, lambda n: n.get('text') == MODULE_LABEL)
        if node is None:
            node = ocr_target(tsv, MODULE_LABEL)
        if node is None:
            node = unique_node(ui, lambda n: n.get('text') == 'Modules' or n.get('content-desc') == 'Modules')
        if node is not None:
            tap(node)
        else:
            time.sleep(2)
    raise RuntimeError(f'Unable to set {MODULE_LABEL} enabled={enabled_state} through the official Manager UI')


def hook_diagnostics(logs):
    return UI_STRATEGY.hook_diagnostics(logs)


def collect_logs():
    logcat = adb('logcat', '-d', '-v', 'threadtime', check=False)
    save('logcat.txt', logcat)
    framework = su('for d in /data/adb/lspd/log /data/adb/lspd/logs; do if [ -d "$d" ]; then find "$d" -type f -exec cat {} +; fi; done', check=False)
    save('lsposed-logs.txt', framework)
    save('framework-state.txt', su('ls -la /data/adb/lspd; ls -la /data/adb/modules/zygisk_lsposed; cat /data/adb/modules/zygisk_lsposed/module.prop; ps -A', check=False))
    return (logcat.stdout + framework.stdout).decode(errors='replace')


def apk_digest(path):
    digest = hashlib.sha256()
    with path.open('rb') as apk:
        for chunk in iter(lambda: apk.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def apk_native_abi(path):
    with zipfile.ZipFile(path) as apk:
        abis = {
            match.group(1)
            for name in apk.namelist()
            if (match := re.fullmatch(r'lib/([^/]+)/[^/]+\.so', name))
        }
    if len(abis) != 1:
        raise ValueError(f'Candidate APK must contain native libraries for exactly one ABI, found {sorted(abis)}')
    abi = next(iter(abis))
    if abi not in APP_POLICY.supported_abis:
        raise ValueError(f'Candidate APK ABI {abi} is unsupported by {APP_ID} policy')
    return abi


def prepare_environment(report):
    global stage
    stage = 'AUTOMATION_SETUP'
    adb('push', str(ROOT / 'ui-hierarchy.jar'), '/data/local/tmp/ui-hierarchy.jar')
    stage = 'BASELINE'
    enable_page_size_backcompat()
    save('page-size-compat.txt', adb('shell', 'getprop'))
    adb('logcat', '-c')
    save(f'{APP_ID}-install.txt', adb('install', '-r', '-g', str(ROOT / f'{APP_ID}.apk'), timeout=240))
    ui = mobile_input('baseline-mobile')
    if any(is_tablet_entry(n.get('text', '')) for n in nodes(ui)):
        raise RuntimeError(f'Policy entry already exists without module; baseline does not establish module effect')
    report['baseline'] = APP_POLICY.runtime_baseline
    stage = 'FRAMEWORK_INSTALL'
    if APP_ID == 'qq' and os.environ.get('QQ_DIAGNOSTIC_LAYER') == 'zygisk-next-lsposed':
        from scripts.ci.qq_sigbus_diagnostic import prepare_layer
        prepare_layer('zygisk-next-lsposed', runtime=sys.modules[__name__])
    else:
        bootstrap_magisk()
        adb('push', str(ROOT / 'lsposed.zip'), '/data/local/tmp/lsposed.zip')
        save('zygisk-setting.txt', su(MAGISK + ' --sqlite "REPLACE INTO settings (key,value) VALUES (\'zygisk\',1)"'))
        result = su('export PATH=/debug_ramdisk:/data/adb/magisk:$PATH; magisk --install-module /data/local/tmp/lsposed.zip', timeout=240, check=False)
        save('lsposed-install.txt', result)
        if result.returncode:
            raise RuntimeError('Official LSPosed installer failed; see lsposed-install.txt')
    module_install = adb('install', '-r', str(ROOT / 'module/app-debug.apk'), timeout=120)
    save('module-install.txt', module_install)
    module_path = adb('shell', 'pm', 'path', MODULE, check=False)
    save('module-package-probe.txt', module_path)
    if module_path.returncode or not module_path.stdout.decode(errors='replace').strip().startswith('package:'):
        raise RuntimeError(f'Installed module package {MODULE} was not found')
    save('manager-install.txt', adb('install', '-r', str(ROOT / 'manager.apk'), timeout=120))
    reboot()
    stage = 'FRAMEWORK_START'
    for _ in range(45):
        daemon = su('pidof lspd', check=False)
        if daemon.returncode == 0 and daemon.stdout.strip():
            save('lspd-pid.txt', daemon)
            break
        time.sleep(2)
    else:
        raise RuntimeError('LSPosed daemon did not start after reboot')
    stage = 'MODULE_ENABLE'
    configure_manager()

def probe_environment(report):
    global stage
    stage = 'HOOK_AND_LOGIN'
    adb('shell', 'pm', 'clear', APP_PACKAGE)
    adb('logcat', '-c')
    ui = mobile_input('module-mobile')
    if APP_ID == 'qq':
        probe_qq_environment(report)
        return
    choice = unique_node(ui, lambda n: is_tablet_entry(n.get('text', '')))
    if choice is None:
        raise RuntimeError(f'No {APP_ID} policy entry after module enable')
    tap(choice)
    for _ in range(20):
        activity, ui = snapshot('qr-current')
        if qr_page_ready(activity, ui):
            time.sleep(3)
            activity, ui = snapshot('qr')
            if qr_page_ready(activity, ui):
                break
        elif APP_ID == 'wecom':
            agreement = unique_node(ui, lambda n: UI_STRATEGY.is_login_navigation_label(n.get('text', '')))
            if agreement is not None:
                tap(agreement)
        time.sleep(2)
    else:
        raise RuntimeError('Tablet login did not reach a stable rendered QR page')
    logs = collect_logs()
    hook_log = getattr(UI_STRATEGY, 'hook_installation_log', '')
    if 'status=COMPATIBLE' not in logs or not hook_log or hook_log not in logs:
        raise RuntimeError('Rendered app page was not accompanied by compatible app hook installation logs')
    apk_path = ROOT / f'{APP_ID}.apk'
    report.update(status='RUNTIME_SMOKE_VERIFIED', stage='COMPLETE', hookIds=list(APP_POLICY.required_hook_ids),
                  hooks=len(APP_POLICY.required_hook_ids), qrPage=APP_POLICY.runtime_qr_page,
                  appId=APP_ID, packageName=APP_PACKAGE, abi=apk_native_abi(apk_path),
                  apkSha256=apk_digest(apk_path))

def probe_qq_environment(report):
    from scripts.ci.apps.qq.runtime_probe import observe_pair, manifest_expectation
    module_sha = apk_digest(ROOT / 'module/app-debug.apk')
    try:
        expectation = manifest_expectation(ROOT / 'qq.apk', apk_digest(ROOT / 'qq.apk'))
        proof = observe_pair(sys.modules[__name__])
    except (RuntimeError, ValueError, OSError) as error:
        report.update(status='ACCEPTANCE_UNAVAILABLE', reason=str(error),
                      acceptanceKind='REQUEST_APP_ID_CONTRAST')
        return
    report.update(requestIdentityEvidence=proof, moduleApkSha256=module_sha,
                  acceptanceKind='REQUEST_APP_ID_CONTRAST')
    if not UI_STRATEGY.validate_request_evidence(proof, apk_digest(ROOT / 'qq.apk'), module_sha, expectation):
        report.update(status='RUNTIME_REJECTED', reason='Request identities did not establish the phone/tablet contrast')
        return
    logs = collect_logs()
    diagnostics = UI_STRATEGY.hook_diagnostics(logs)
    if diagnostics['compatibility'] != 'COMPATIBLE' or diagnostics['hookInstallation'] != 'INSTALLED_1':
        report.update(status='RUNTIME_REJECTED', reason='Request contrast has no compatible hook installation evidence')
        return
    report.update(status='RUNTIME_SMOKE_VERIFIED', stage='COMPLETE',
                  hookIds=list(APP_POLICY.required_hook_ids), hooks=len(APP_POLICY.required_hook_ids),
                  appId=APP_ID, packageName=APP_PACKAGE, abi=apk_native_abi(ROOT / 'qq.apk'),
                  apkSha256=apk_digest(ROOT / 'qq.apk'))


def main(mode="full", app_id=None):
    global stage
    if app_id is not None:
        configure_app_id(app_id)
    apk_path = ROOT / f'{APP_ID}.apk'
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    report = {'status': 'FAILED', 'stage': stage, 'appId': APP_ID}
    if apk_path.is_file():
        try:
            report.update(packageName=APP_PACKAGE, abi=apk_native_abi(apk_path), apkSha256=apk_digest(apk_path))
        except (OSError, ValueError, zipfile.BadZipFile) as error:
            report.update(packageName=APP_PACKAGE, apkSha256=apk_digest(apk_path), identityError=str(error))
    try:
        if mode in {'full', 'prepare'}:
            prepare_environment(report)
        if mode in {'full', 'probe'}:
            if mode == 'probe':
                if not (ROOT / 'prepared.json').is_file():
                    raise RuntimeError('Runtime environment has not passed baseline preparation')
            report['baseline'] = APP_POLICY.runtime_baseline
            probe_environment(report)
        else:
            report.update(status='ENVIRONMENT_READY', stage='READY')
            (ROOT / 'prepared.json').write_text(json.dumps(report) + '\n')
    except Exception as error:
        report.update(stage=stage, reason=str(error))
        try:
            snapshot('failure')
        except Exception as diagnostic:
            report['diagnosticError'] = str(diagnostic)
    finally:
        collect_logs()
        logcat = EVIDENCE / 'logcat.txt'
        report['runtimeDiagnostics'] = hook_diagnostics(logcat.read_text(errors='replace') if logcat.is_file() else '')
        (EVIDENCE / 'smoke-report.json').write_text(json.dumps(report, indent=2) + '\n')
        (EVIDENCE / 'summary.md').write_text(f'# {APP_ID} runtime smoke\n\n' + json.dumps(report, indent=2)
                                            + '\n\nNo QR scanning, account login or dual-device session was performed.\n')
        print(json.dumps(report))
    return 0 if report['status'] in {'RUNTIME_SMOKE_VERIFIED', 'CONFIGURATION_SMOKE_VERIFIED', 'ENVIRONMENT_READY'} else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--app-id', default='wechat')
    args = parser.parse_args()
    raise SystemExit(main(app_id=args.app_id))
