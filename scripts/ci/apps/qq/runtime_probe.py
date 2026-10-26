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
import shlex
import threading
from pathlib import PurePosixPath
from contextlib import contextmanager
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


def manager_state_evidence(smoke):
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
            enabled = connection.execute(
                'SELECT module_pkg_name, user_id, enabled FROM modules_state WHERE module_pkg_name=? AND user_id=0',
                (smoke.MODULE,),
            ).fetchall()
            scope = connection.execute(
                'SELECT module_pkg_name, app_pkg_name, user_id FROM scope WHERE module_pkg_name=? AND app_pkg_name=? AND user_id=0',
                (smoke.MODULE, smoke.APP_PACKAGE),
            ).fetchall()
        finally:
            connection.close()
    if len(enabled) != 1 or enabled[0][2] not in (0, 1) or not scope:
        raise RuntimeError('QQ module state or LSPosed scope is missing')
    return {
        'schemaVersion': 1,
        'module': smoke.MODULE,
        'packageName': smoke.APP_PACKAGE,
        'moduleStateRows': [
            {'modulePackage': row[0], 'userId': row[1], 'enabled': row[2]}
            for row in enabled
        ],
        'scopeRows': [
            {'modulePackage': row[0], 'appPackage': row[1], 'userId': row[2]}
            for row in scope
        ],
    }


def manager_state(smoke):
    raw = manager_state_evidence(smoke)
    row = raw['moduleStateRows'][0]
    return {'moduleEnabled': bool(row['enabled']), 'scopeVerified': bool(raw['scopeRows'])}


def installed_hash(smoke, package):
    rows = smoke.adb('shell', 'pm', 'path', package).stdout.decode().splitlines()
    base = [row.removeprefix('package:') for row in rows if row.endswith('/base.apk')]
    if len(base) != 1:
        raise ValueError('Installed base APK is ambiguous')
    return smoke.adb('shell', 'sha256sum', base[0]).stdout.decode().split()[0]


def installed_identity(smoke, package):
    rows = smoke.adb('shell', 'pm', 'path', package).stdout.decode().splitlines()
    if any(not row.startswith('package:') for row in rows):
        raise ValueError('Installed APK path output is malformed')
    paths = [row.removeprefix('package:') for row in rows]
    if any(not PurePosixPath(path).is_absolute() or '..' in PurePosixPath(path).parts
           or not path.endswith('.apk') for path in paths):
        raise ValueError('Installed APK path is malformed')
    base = [path for path in paths if path.endswith('/base.apk')]
    if len(base) != 1:
        raise ValueError('Installed base APK is ambiguous')

    def installed_digest(path):
        fields = smoke.adb('shell', 'sha256sum', path).stdout.decode().split()
        if len(fields) < 2 or fields[1] != path or not re.fullmatch(r'[a-fA-F0-9]{64}', fields[0]):
            raise ValueError('Installed APK digest output is malformed')
        return fields[0].lower()

    identity = {'packageName': package, 'baseApkPath': base[0], 'baseApkSha256': installed_digest(base[0])}
    splits = [path for path in paths if path != base[0]]
    if len(paths) > 1:
        if len(set(paths)) != len(paths):
            raise ValueError('Installed APK paths are duplicated')
        identity['splitApks'] = [{'path': path, 'sha256': installed_digest(path)} for path in splits]
    return identity


def navigate(smoke):
    _, xml = smoke.snapshot('qq-request-navigation')
    nodes = smoke.nodes(xml)
    if any(n.get('package') == 'android' and n.get('text') == 'Allow QQ to access all device logs?' for n in nodes):
        deny = smoke.unique_node(xml, lambda n: n.get('package') == 'android'
                                 and n.get('text') in {'Don’t allow', "Don't allow"}
                                 and n.get('clickable') == 'true' and n.get('enabled') != 'false')
        if deny is not None:
            smoke.tap(deny)
        return
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
    priority = ['QID/邮箱登录', '账密登录', 'Log in with password', '其他方式登录', 'Other login methods', '同意', 'Agree', '登录', 'Log in', 'Login']
    for label in priority:
        if label not in labels:
            continue
        target = smoke.unique_node(xml, lambda n: n.get('package') == smoke.APP_PACKAGE and n.get('enabled') != 'false' and n.get('text', '').strip() == label)
        if target is not None:
            smoke.tap(target)
            return


def configure_manager(smoke, enabled):
    _, xml = smoke.snapshot('qq-manager-preflight')
    if any(n.get('text') == 'QQ keeps stopping' for n in smoke.nodes(xml)):
        close = smoke.unique_node(xml, lambda n: n.get('text') == 'Close app' and n.get('package') == 'android')
        if close is not None:
            smoke.tap(close)
    smoke.configure_manager(enabled)


def collect_process_diagnostics(smoke, enabled):
    label = 'enabled' if enabled else 'disabled'
    smoke.save('qq-process-' + label + '.txt', smoke.adb(
        'shell', 'dumpsys', 'activity', 'exit-info', smoke.APP_PACKAGE, timeout=25, check=False))
    smoke.save('qq-signals-' + label + '.txt', smoke.adb(
        'logcat', '-d', '-v', 'threadtime', '-s', 'libsigchain:E', 'libc:F',
        'DEBUG:F', timeout=25, check=False))


@contextmanager
def isolated_tcp(smoke, uid):
    if smoke.adb('shell', 'getprop', 'ro.kernel.qemu').stdout.strip() != b'1':
        raise RuntimeError('QQ TCP isolation requires a dedicated emulator')
    chain = 'IMPAD_QQ_CAPTURE'
    created, attached = [], []
    try:
        for tool in ('iptables', 'ip6tables'):
            if smoke.su(f'{tool} -w -S {chain}', check=False).returncode == 0:
                raise RuntimeError('An unowned QQ isolation chain already exists')
            smoke.su(f'{tool} -w -N {chain}')
            created.append(tool)
            smoke.su(f'{tool} -w -A {chain} -m owner --uid-owner {uid} -j RETURN')
            smoke.su(f'{tool} -w -A {chain} -j DROP')
            smoke.su(f'{tool} -w -I OUTPUT 1 ! -o lo -p tcp -j {chain}')
            attached.append(tool)
            smoke.save('qq-tcp-isolation-' + tool + '.txt', smoke.su(f'{tool} -w -S {chain}'))
        yield
    finally:
        failures = []
        for tool in reversed(created):
            commands = ([f'{tool} -w -D OUTPUT ! -o lo -p tcp -j {chain}'] if tool in attached else [])
            commands += [f'{tool} -w -F {chain}', f'{tool} -w -X {chain}']
            for command in commands:
                try:
                    smoke.su(command)
                except (RuntimeError, OSError, subprocess.SubprocessError) as error:
                    failures.append(str(error))
        if failures:
            raise RuntimeError('QQ TCP isolation restoration failed: ' + '; '.join(failures))


def observe(smoke, enabled):
    from scripts.ci.apps.qq.packet_capture import TcpCapture, observer_source_digest
    from scripts.ci.apps.qq.prepare_probe import NDK_VERSION
    root = smoke.ROOT
    expected_hash = smoke.apk_digest(root / 'qq.apk')
    if installed_hash(smoke, smoke.APP_PACKAGE) != expected_hash:
        raise ValueError('Installed QQ differs from the exact candidate')
    configure_manager(smoke, enabled)
    state = manager_state(smoke)
    if state['moduleEnabled'] is not enabled:
        raise RuntimeError('LSPosed did not persist the requested module state')
    package = smoke.adb('shell', 'cmd', 'package', 'list', 'packages', '-U', smoke.APP_PACKAGE).stdout.decode()
    matches = re.findall(r'^package:com\.tencent\.mobileqq uid:([0-9]+)$', package, re.MULTILINE)
    if len(matches) != 1 or not 10000 <= int(matches[0]) <= 0x7fffffff:
        raise RuntimeError('Cannot independently resolve QQ UID')
    uid = int(matches[0])
    abi = smoke.adb('shell', 'getprop', 'ro.product.cpu.abi').stdout.decode().strip()
    metadata = json.loads((root / 'qq-probe/collector.json').read_text())
    binary = root / 'qq-probe/collector'
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    if (metadata.get('ndkVersion') != NDK_VERSION or metadata.get('observerSha256') != observer_source_digest() or metadata.get('binarySha256') != digest
            or metadata.get('abi') != {'arm64-v8a':'arm64','x86_64':'x86_64'}.get(abi)):
        raise ValueError('Prepared collector differs from trusted sources or device ABI')
    smoke.enable_page_size_backcompat()
    smoke.adb('shell', 'am', 'force-stop', smoke.APP_PACKAGE)
    for _ in range(30):
        if not smoke.adb('shell', 'pidof', 'com.tencent.mobileqq:MSF', check=False).stdout.strip():
            break
        time.sleep(.2)
    else:
        raise RuntimeError('Previous QQ MSF did not stop')
    with isolated_tcp(smoke, uid):
        remote = '/data/local/tmp/impad-qq-collector'
        if smoke.su('pidof impad-qq-collector', check=False).stdout.strip():
            raise RuntimeError('An unowned QQ collector is already running')
        pid_file = remote + '.pid'
        capture = TcpCapture(uid)
        ready = threading.Event()
        client = reader = None
        (smoke.EVIDENCE / 'qq-collector-error.txt').unlink(missing_ok=True)
        errors = tempfile.TemporaryFile()
        label = ('enabled' if enabled else 'disabled') + '-' + str(time.time_ns())
        raw_events = (smoke.EVIDENCE / ('qq-collector-events-' + label + '.jsonl')).open('wb')
        try:
            smoke.adb('push', str(binary), remote)
            smoke.su('chmod 700 ' + remote)
            remote_hash = smoke.su('sha256sum ' + remote).stdout.decode().split()[0]
            if remote_hash != digest:
                raise ValueError('Installed collector differs from prepared binary')
            smoke.su('rm -f ' + pid_file)
            command = 'exec ' + remote + ' ' + str(uid) + ' 45 ' + pid_file
            serial = os.environ.get('ANDROID_SERIAL')
            adb_args = ['adb'] + (['-s', serial] if serial else [])
            shell = smoke.MAGISK + ' su -c ' + shlex.quote(command)
            client = subprocess.Popen(adb_args + ['shell', '-T', shell], stdout=subprocess.PIPE, stderr=errors)
            def read_events():
                recorded = 0
                try:
                    while True:
                        line = client.stdout.readline(132000)
                        if not line:
                            break
                        if recorded + len(line) <= 16 * 1024 * 1024:
                            raw_events.write(line)
                            recorded += len(line)
                        if not line.endswith(b'\n'):
                            capture.errors.append('Kernel capture record exceeded limit')
                            break
                        capture.feed(json.loads(line))
                        if capture.ready:
                            ready.set()
                except (ValueError, OSError):
                    capture.errors.append('Invalid kernel capture stream')
            reader = threading.Thread(target=read_events, daemon=True)
            reader.start()
            deadline = time.monotonic() + 60
            while not ready.wait(.2):
                if client.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError('Kernel collector did not become ready')
            smoke.adb('shell', 'am', 'start', '-n', smoke.UI_STRATEGY.launcher_component)
            while client.poll() is None:
                if time.monotonic() > deadline:
                    raise RuntimeError('Kernel collector timed out')
                navigate(smoke)
                time.sleep(1)
            reader.join(timeout=5)
            if reader.is_alive() or client.returncode:
                raise RuntimeError('Kernel collector failed or did not drain')
            observed = capture.result()
            (smoke.EVIDENCE / ('qq-capture-enabled.json' if enabled else 'qq-capture-disabled.json')).write_text(json.dumps(observed, indent=2) + '\n')
            if not observed['captureHealthy']:
                raise RuntimeError('Kernel capture incomplete: ' + '; '.join(observed['errors']))
            if manager_state(smoke) != state or installed_hash(smoke, smoke.APP_PACKAGE) != expected_hash:
                raise ValueError('QQ state or APK changed during capture')
            if smoke.su('sha256sum ' + remote).stdout.decode().split()[0] != digest:
                raise ValueError('Collector changed during capture')
            return dict(observed, **state, observerSha256=observer_source_digest(),
                        collectorBinarySha256=digest, ndkVersion=NDK_VERSION, apkSha256=expected_hash, appUid=uid,
                        moduleApkSha256=installed_hash(smoke, smoke.MODULE), processName='com.tencent.mobileqq:MSF')
        finally:
            recovered = smoke.su('cat ' + pid_file, check=False).stdout.decode().strip()
            identity = re.fullmatch(r'([1-9][0-9]*) ([1-9][0-9]*)', recovered)
            if identity and int(identity[1]) > 1:
                pid, birth = identity.groups()
                executable = smoke.su('readlink /proc/' + pid + '/exe', check=False).stdout.decode().strip()
                stat = smoke.su('cat /proc/' + pid + '/stat', check=False).stdout.decode()
                fields = stat.rsplit(')', 1)[-1].split()
                if executable == remote and len(fields) > 19 and fields[19] == birth:
                    smoke.su('kill ' + pid, check=False)
            if client is not None and client.poll() is None:
                client.terminate()
                try:
                    client.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    client.kill(); client.wait(timeout=5)
            if reader is not None:
                reader.join(timeout=5)
            raw_events.close()
            if client is not None and client.stdout is not None:
                client.stdout.close()
            errors.seek(0)
            diagnostic = errors.read().decode(errors='replace')[-65536:]
            if diagnostic:
                (smoke.EVIDENCE / 'qq-collector-error.txt').write_text(diagnostic)
                (smoke.EVIDENCE / ('qq-collector-diagnostic-' + label + '.txt')).write_text(diagnostic)
            if client is not None and client.returncode:
                smoke.save('qq-sockets-' + label + '.txt', smoke.su(
                    'cat /proc/net/tcp /proc/net/tcp6; ps -A; '
                    'for p in $(pidof com.tencent.mobileqq:MSF); do cat /proc/$p/stat; ls -l /proc/$p/fd; done', check=False))
            errors.close()
            smoke.su('rm -f ' + pid_file + ' ' + remote, check=False)
            try:
                collect_process_diagnostics(smoke, enabled)
            except (RuntimeError, OSError) as error:
                (smoke.EVIDENCE / ('qq-process-' + ('enabled' if enabled else 'disabled') + '-error.txt')).write_text(str(error))


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
