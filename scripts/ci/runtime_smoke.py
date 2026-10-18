"""Verify official LSPosed and WeChat login UI on a disposable hosted ReDroid."""

import hashlib
import base64
import json
import os
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
MAGISK = os.environ.get('IMPAD_MAGISK_PATH', '/debug_ramdisk/magisk')
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
    return run(os.environ.get('IMPAD_ADB_BINARY', 'adb'), *args, timeout=timeout, check=check)


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
    normalized_label = re.sub(r'[^\w]+', ' ', label.casefold()).strip()
    for line in lines.values():
        for start in range(len(line)):
            for end in range(start + 1, len(line) + 1):
                words = line[start:end]
                text = ' '.join(word['text'] for word in words)
                normalized_text = re.sub(r'[^\w]+', ' ', text.casefold()).strip()
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


def launcher_component():
    component = getattr(UI_STRATEGY, 'launcher_component', None)
    if component:
        return component
    output = adb('shell', 'cmd', 'package', 'resolve-activity', '--brief',
                 '-a', 'android.intent.action.MAIN', '-c',
                 'android.intent.category.LAUNCHER', APP_PACKAGE).stdout.decode().splitlines()
    components = [line.strip() for line in output if line.strip().startswith(APP_PACKAGE + '/')]
    if len(components) != 1:
        raise RuntimeError('App launcher component is missing or ambiguous')
    return components[0]


def mobile_input(label):
    enable_page_size_backcompat()
    adb('shell', 'am', 'force-stop', APP_PACKAGE)
    adb('shell', 'am', 'start', '-n', launcher_component())
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


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Shared runtime UI helpers; use the ReDroid controller for acceptance.')
    parser.add_argument('--app-id', default='wechat')
    parser.parse_args()
    parser.error('Run scripts.ci.redroid_environment_probe with prepared application inputs')
