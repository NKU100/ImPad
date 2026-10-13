"""Trusted, profile-independent QQ request identity observation."""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET
from scripts.ci.apps.qq.request_protocol import CONTRACT, SECURITY_COMMANDS

ANDROID = '{http://schemas.android.com/apk/res/android}'


def manifest_app_ids(xml):
    values = {}
    for node in ET.fromstring(xml).iter('meta-data'):
        key = node.get(ANDROID + 'name')
        if key not in ('AppSetting_params', 'AppSetting_params_pad'):
            continue
        if key in values:
            raise ValueError('Duplicate QQ Manifest configuration')
        value = node.get(ANDROID + 'value', '').split('#')[0]
        if not re.fullmatch(r'[1-9][0-9]*', value):
            raise ValueError('Invalid QQ Manifest AppID')
        values[key] = int(value)
    phone, tablet = values['AppSetting_params'], values['AppSetting_params_pad']
    if phone == tablet or max(phone, tablet) > 0x7fffffff:
        raise ValueError('QQ phone/tablet AppIDs must be distinct valid integers')
    return phone, tablet


def manifest_expectation(apk, expected_sha256):
    from scripts.ci.fetch_latest_wechat_apk import file_sha256
    apk = Path(apk)
    if file_sha256(apk) != expected_sha256:
        raise ValueError('Manifest APK differs from the trusted candidate')
    sdk = Path(os.environ.get('ANDROID_HOME') or os.environ.get('ANDROID_SDK_ROOT', ''))
    xml = subprocess.check_output([str(sdk / 'cmdline-tools/latest/bin/apkanalyzer'),
                                   'manifest', 'print', str(apk)], timeout=60).decode()
    phone, tablet = manifest_app_ids(xml)
    if file_sha256(apk) != expected_sha256:
        raise ValueError('Manifest APK changed during extraction')
    return {'contract': CONTRACT, 'apkSha256': expected_sha256,
            'phoneAppId': phone, 'tabletAppId': tablet}


def manager_state(smoke):
    with tempfile.TemporaryDirectory() as directory:
        database = Path(directory) / 'modules_config.db'
        for suffix in ('', '-wal'):
            result = smoke.su('base64 /data/adb/lspd/config/modules_config.db' + suffix, check=False)
            if result.returncode == 0:
                Path(str(database) + suffix).write_bytes(base64.b64decode(b''.join(result.stdout.split()), validate=True))
            elif not suffix:
                raise RuntimeError('Cannot independently read LSPosed module state')
        connection = sqlite3.connect(database)
        try:
            enabled = connection.execute('SELECT enabled FROM modules_state WHERE module_pkg_name=? AND user_id=0', (smoke.MODULE,)).fetchall()
            scope = connection.execute('SELECT 1 FROM scope WHERE module_pkg_name=? AND app_pkg_name=? AND user_id=0', (smoke.MODULE, smoke.APP_PACKAGE)).fetchall()
        finally:
            connection.close()
    if len(enabled) != 1 or enabled[0][0] not in (0, 1) or not scope:
        raise RuntimeError('QQ module state or LSPosed scope is missing')
    return {'moduleEnabled': bool(enabled[0][0]), 'scopeVerified': True}


def installed_hash(smoke, package):
    rows = smoke.adb('shell', 'pm', 'path', package).stdout.decode().splitlines()
    base = [row.removeprefix('package:') for row in rows if row.endswith('/base.apk')]
    if len(base) != 1:
        raise ValueError('Installed base APK is ambiguous')
    return smoke.adb('shell', 'sha256sum', base[0]).stdout.decode().split()[0]


def navigate(smoke):
    _, xml = smoke.snapshot('qq-request-navigation')
    nodes = smoke.nodes(xml)
    texts = [n.get('text', '') + ' ' + n.get('content-desc', '') for n in nodes]
    if any(n.get('password') == 'true' or ('EditText' in n.get('class', '') and ('password' in texts[i].lower() or '\u5bc6\u7801' in texts[i])) or '\u8f93\u5165QQ\u5bc6\u7801' in texts[i] for i, n in enumerate(nodes)):
        return
    inputs = any('EditText' in n.get('class', '') or n.get('password') == 'true' for n in nodes)
    labels = {'\u8d26\u5bc6\u767b\u5f55', '\u5176\u4ed6\u65b9\u5f0f\u767b\u5f55', 'QID/\u90ae\u7bb1\u767b\u5f55',
              'Other login methods', 'Log in with password'}
    welcome = any(n.get('text', '').strip() in {'\u6ce8\u518c', '\u65b0\u7528\u6237', 'Register', 'New user'} for n in nodes)
    if not inputs:
        labels |= {'Agree', '\u540c\u610f'}
        if welcome:
            labels |= {'\u767b\u5f55', 'Log in', 'Login'}
    target = smoke.unique_node(xml, lambda n: n.get('package') == smoke.APP_PACKAGE and
                              n.get('enabled') != 'false' and n.get('text', '').strip() in labels)
    if target is not None:
        smoke.tap(target)


def configure_manager(smoke, enabled):
    _, xml = smoke.snapshot('qq-manager-preflight')
    if any(n.get('text') == 'QQ keeps stopping' for n in smoke.nodes(xml)):
        close = smoke.unique_node(xml, lambda n: n.get('text') == 'Close app' and n.get('package') == 'android')
        if close is not None:
            smoke.tap(close)
    smoke.configure_manager(enabled)


def observe(smoke, enabled):
    root = smoke.ROOT
    system_abi = smoke.adb('shell', 'getprop', 'ro.product.cpu.abi').stdout.decode().strip()
    realm = 'emulated' if system_abi in ('x86', 'x86_64') else 'native'
    expected_hash = smoke.apk_digest(root / 'qq.apk')
    if installed_hash(smoke, smoke.APP_PACKAGE) != expected_hash:
        raise ValueError('Installed QQ differs from the exact candidate')
    configure_manager(smoke, enabled)
    state = manager_state(smoke)
    if state['moduleEnabled'] is not enabled:
        raise RuntimeError('LSPosed did not persist the requested module state')
    smoke.adb('shell', 'am', 'force-stop', smoke.APP_PACKAGE)
    for _ in range(30):
        if not smoke.adb('shell', 'pidof', 'com.tencent.mobileqq:MSF', check=False).stdout.strip():
            break
        time.sleep(0.2)
    else:
        raise RuntimeError('Previous QQ MSF did not stop')
    smoke.adb('shell', 'am', 'start', '-n', smoke.UI_STRATEGY.launcher_component)
    previous_pid = None
    stable = 0
    for _ in range(30):
        pids = smoke.adb('shell', 'pidof', 'com.tencent.mobileqq:MSF', check=False).stdout.decode().split()
        if len(pids) == 1:
            stable = stable + 1 if pids[0] == previous_pid else 1
            previous_pid = pids[0]
            if stable >= 6:
                break
        else:
            previous_pid, stable = None, 0
        time.sleep(1)
    else:
        raise RuntimeError('QQ MSF did not start')
    remote = '/data/local/tmp/impad-qq-wire-server'
    if smoke.su('pidof impad-qq-wire-server', check=False).stdout.strip():
        raise RuntimeError('An unowned QQ wire observer is already running')
    smoke.adb('push', str(root / 'qq-probe/frida-server'), remote)
    smoke.su('chmod 700 ' + remote)
    pid_file = '/data/local/tmp/impad-qq-wire.pid'
    smoke.su('rm -f ' + pid_file)
    port = server_pid = client = None
    ready = smoke.EVIDENCE / 'qq-wire-ready'
    ready.unlink(missing_ok=True)
    try:
        server_pid = smoke.su(f'{remote} -l 127.0.0.1:27043 </dev/null >/data/local/tmp/impad-qq-wire.log 2>&1 & echo $! >{pid_file}; cat {pid_file}').stdout.decode().strip()
        if not re.fullmatch(r'[1-9][0-9]*', server_pid) or int(server_pid) <= 1:
            raise RuntimeError('Observer server PID is invalid')
        port = smoke.adb('forward', 'tcp:0', 'tcp:27043').stdout.decode().strip()
        if not port.isdigit() or not 0 < int(port) < 65536:
            raise RuntimeError('Observer forwarding port is invalid')
        output = tempfile.TemporaryFile()
        errors = tempfile.TemporaryFile()
        try:
            client = subprocess.Popen([str(root / 'qq-probe/venv/bin/python'), '-m',
                                       'scripts.ci.apps.qq.probe_client', '--port', port,
                                       '--pid', pids[0], '--agent', str(root / 'qq-probe/agent.js'),
                                       '--realm', realm,
                                       '--ready', str(ready), '--seconds', '30'], stdout=output, stderr=errors)
            deadline = time.monotonic() + 45
            while not ready.exists():
                if client.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError('Network observer did not become ready')
                time.sleep(0.2)
            while client.poll() is None:
                if time.monotonic() > deadline:
                    raise RuntimeError('Network observer timed out')
                navigate(smoke)
                time.sleep(1)
            output.seek(0)
            if client.returncode:
                errors.seek(0)
                diagnostic = errors.read().decode(errors='replace')[-2000:]
                (smoke.EVIDENCE / 'qq-wire-client-error.txt').write_text(diagnostic)
                raise RuntimeError('Network observer failed: ' + diagnostic.splitlines()[-1] if diagnostic.splitlines() else 'Network observer failed')
            observed = json.loads(output.read())
        except Exception:
            errors.seek(0)
            diagnostic = errors.read().decode(errors='replace')[-4000:]
            if diagnostic:
                (smoke.EVIDENCE / 'qq-wire-client-error.txt').write_text(diagnostic)
            raise
        finally:
            if client is not None and client.poll() is None:
                client.terminate()
                try:
                    client.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    client.kill()
                    client.wait(timeout=5)
            output.close()
            errors.close()
        if manager_state(smoke) != state or installed_hash(smoke, smoke.APP_PACKAGE) != expected_hash:
            raise ValueError('QQ state or APK changed during capture')
        if not observed.get('captureHealthy'):
            (smoke.EVIDENCE / 'qq-wire-incomplete.json').write_text(json.dumps(observed, indent=2) + '\n')
            raise RuntimeError('Network capture was incomplete: ' + '; '.join(observed.get('errors', [])))
        agent_hash = hashlib.sha256(Path(__file__).with_name('probe').joinpath('agent.js').read_bytes()).hexdigest()
        if hashlib.sha256((root / 'qq-probe/agent.js').read_bytes()).hexdigest() != agent_hash:
            raise ValueError('Staged observer differs from the trusted source')
        return dict(observed, **state, observerSha256=agent_hash, apkSha256=expected_hash,
                    moduleApkSha256=installed_hash(smoke, smoke.MODULE), processName='com.tencent.mobileqq:MSF')
    finally:
        server_log = smoke.su('tail -c 4000 /data/local/tmp/impad-qq-wire.log', check=False)
        (smoke.EVIDENCE / ('qq-wire-server-enabled.txt' if enabled else 'qq-wire-server-disabled.txt')).write_text(server_log.stdout.decode(errors='replace'))
        if port and port.isdigit():
            smoke.adb('forward', '--remove', 'tcp:' + port, check=False)
        recovered = smoke.su('cat ' + pid_file, check=False).stdout.decode().strip()
        if re.fullmatch(r'[1-9][0-9]*', recovered) and int(recovered) > 1:
            smoke.su('kill ' + recovered, check=False)
        smoke.su('rm -f ' + pid_file, check=False)
        ready.unlink(missing_ok=True)


def observe_pair(smoke):
    expectation = manifest_expectation(smoke.ROOT / 'qq.apk', smoke.apk_digest(smoke.ROOT / 'qq.apk'))
    phone, tablet = expectation['phoneAppId'], expectation['tabletAppId']
    attempts = []
    try:
        for attempt in range(2):
            off, on = observe(smoke, False), observe(smoke, True)
            attempts.append({'disabled': off, 'enabled': on})
            (smoke.EVIDENCE / f'qq-request-attempt-{attempt + 1}.json').write_text(json.dumps(attempts[-1], indent=2) + '\n')
            common = {f['command'] for f in off['frames']} & {f['command'] for f in on['frames']} & SECURITY_COMMANDS
            if common:
                return {'contract': CONTRACT, 'phoneAppId': phone, 'tabletAppId': tablet,
                        'disabled': off, 'enabled': on}
        raise RuntimeError('No matching trusted security command in the phone/tablet captures')
    finally:
        cleanup_errors = []
        for action in (lambda: configure_manager(smoke, True),
                       lambda: smoke.adb('shell', 'am', 'force-stop', smoke.APP_PACKAGE),
                       lambda: smoke.adb('shell', 'am', 'start', '-n', smoke.UI_STRATEGY.launcher_component)):
            try:
                action()
            except Exception as error:
                cleanup_errors.append(str(error))
        if cleanup_errors:
            raise RuntimeError('QQ state restoration failed; acceptance is unavailable: ' + '; '.join(cleanup_errors))
