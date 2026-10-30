"""Run strict application acceptance in a disposable hosted ARM64 ReDroid."""
from contextlib import contextmanager
import hashlib
import json
from scripts.ci.apk_inputs import resolve_apk_inputs, verify_installed_apk_hashes
import os
from pathlib import Path
import re
import shlex
import subprocess
import threading
import time

from scripts.ci import runtime_smoke as smoke
from scripts.ci.apps.qq import packet_capture, prepare_probe, request_protocol, runtime_policy, runtime_probe
from scripts.ci.redroid_environment_probe import Probe, SERIAL, CONTAINER, MAGISK, checked_digest
from scripts.ci.semantic_hook_evidence import derive_semantic_hook_evidence

FINGERPRINT = 'redroid/redroid_arm64_only/redroid_arm64_only:14/UD2A.240505.001.W1/eng.frank.20240527.152423:userdebug/test-keys'
_THREADTIME_HEADER = re.compile(
    r'^(\d{2}-\d{2})\s+(\d{2}:\d{2}:\d{2}\.\d{3})\s+'
    r'(\d+)\s+(\d+)\s+([VDIWEF])\s+([^\s:]+):(?:\s+(.*))?$'
)
_RUNTIME_EVENT = re.compile(r'rule-runtime event=([a-z_]+)(?:\s+(.*))?$')
_LSPOSED_RUNTIME_EVENT = re.compile(
    r'\(com\.tencent\.mobileqq:MSF\)'
    r'\[io\.github\.nku100\.impad,ImPad,[A-Za-z0-9_.-]+,\d+,\d+\]'
    r'\s+(rule-runtime event=[a-z_]+(?:\s+.*)?)$'
)


def extract_qq_lifecycle_evidence(logs, owner_pids, request_times):
    """Read legacy rule-ID lifecycle logs for pinned v1 replay fixtures only."""
    events = {}
    for line in logs.splitlines():
        header = _THREADTIME_HEADER.fullmatch(line)
        if not header or int(header.group(3)) not in owner_pids:
            continue
        tag, message = header.group(6), header.group(7) or ''
        if tag == 'ImPad':
            event_message = message
        elif tag == 'LSPosedFramework':
            envelope = _LSPOSED_RUNTIME_EVENT.fullmatch(message)
            if not envelope:
                continue
            event_message = envelope.group(1)
        else:
            continue
        match = _RUNTIME_EVENT.fullmatch(event_message)
        if not match:
            continue
        fields = dict(part.split('=', 1) for part in (match.group(2) or '').split() if '=' in part)
        events.setdefault(match.group(1), []).append(fields)
    qfix = events.get('qfix_ready', [])
    native = events.get('native_loaded', [])
    hook = events.get('hook_installed', [])
    qfix_ok = len(qfix) == 1 and qfix[0].get('contextPackage') == 'com.tencent.mobileqq' \
        and qfix[0].get('loaderClass') and qfix[0].get('elapsedRealtimeNs', '').isdigit()
    native_ok = len(native) == 1 and all(re.fullmatch(r'[a-f0-9]{64}', native[0].get(key, ''))
                                         for key in ('apiSha256', 'nativeSha256')) \
        and native[0].get('elapsedRealtimeNs', '').isdigit()
    hook_ok = len(hook) == 1 and hook[0].get('ruleId') == 'qq_device_type_001' \
        and hook[0].get('strategyId') == 'qq_tablet_001' and hook[0].get('hookCount') == '1' \
        and hook[0].get('processName') == 'com.tencent.mobileqq:MSF' \
        and hook[0].get('elapsedRealtimeNs', '').isdigit()
    qfix_before_native = qfix_ok and native_ok and int(qfix[0]['elapsedRealtimeNs']) < int(native[0]['elapsedRealtimeNs'])
    request_ns = [value for value in request_times if type(value) is int and value > 0]
    hook_before_request = hook_ok and bool(request_ns) \
        and int(hook[0]['elapsedRealtimeNs']) < min(request_ns)
    return {
        'nativeEvidence': ({'status': 'LOADED', 'apiSha256': native[0]['apiSha256'],
                            'nativeSha256': native[0]['nativeSha256']} if native_ok else {'status': 'NOT_OBSERVED'}),
        'qfixEvidence': ({'status': 'READY', 'contextPackage': qfix[0]['contextPackage'],
                          'classLoader': qfix[0]['loaderClass']} if qfix_ok else {'status': 'NOT_OBSERVED'}),
        'timingEvidence': {'qfixReadyBeforeNative': bool(qfix_before_native),
                           'hookInstalledBeforeRequest': bool(hook_before_request)},
    }


def install_candidate(adb, root, candidate):
    paths = resolve_apk_inputs(candidate, root)
    command = 'install-multiple' if len(paths) > 1 else 'install'
    adb.adb(command, '-r', *(str(path) for path in paths), timeout=180)


def qq_compatible_hook_installed(logs: str, owner_pids):
    """Read legacy compatibility markers for pinned v1 replay fixtures only."""
    owner_pids = {str(pid) for pid in owner_pids}
    compatible, installed = set(), set()
    for line in logs.splitlines():
        fields = line.split()
        if len(fields) <= 5 or fields[2] not in owner_pids:
            continue
        if fields[5].rstrip(':') not in ('ImPad', 'LSPosedFramework'):
            continue
        if 'status=COMPATIBLE' in line:
            compatible.add(fields[2])
        if 'installed 1 QQ hooks' in line:
            installed.add(fields[2])
    return bool(compatible & installed)


def capture_qq_process_snapshot(smoke_client, evidence_directory, owner_pids):
    """Persist the device ps snapshot and bind one current MSF owner to captured packets."""
    raw = smoke_client.su('ps -A -o PID,NAME').stdout
    process_text = raw.decode(errors='strict') if isinstance(raw, bytes) else raw
    if not isinstance(process_text, str):
        raise ValueError('QQ process snapshot is not text')
    rows = []
    for line in process_text.splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[0].isdigit() and fields[1] == 'com.tencent.mobileqq:MSF':
            rows.append(int(fields[0]))
    owners = set(owner_pids)
    if len(rows) != 1 or owners != {rows[0]}:
        raise ValueError('QQ capture must have exactly one current MSF owner matching packet ownership')
    (Path(evidence_directory) / 'processes-enabled.txt').write_text(process_text)
    return process_text


def extract_qq_semantic_lifecycle_evidence(logs, owner_pids, request_times, candidate,
                                           module, process_text, installed_module=None):
    """Decode v2 lifecycle evidence scoped to the one verified current QQ MSF owner."""
    process_rows = []
    for line in process_text.splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[0].isdigit():
            pid = int(fields[0])
            if fields[1] == 'com.tencent.mobileqq:MSF':
                process_rows.append(pid)
    owners = set(owner_pids)
    if len(process_rows) != 1 or owners != set(process_rows):
        raise ValueError('QQ lifecycle owner differs from the unique current MSF process')

    scoped = []
    for line in logs.splitlines():
        header = _THREADTIME_HEADER.fullmatch(line)
        if header and int(header.group(3)) in owners and header.group(6) in {'ImPad', 'LSPosedFramework'}:
            scoped.append(line)
    scoped_logs = '\n'.join(scoped)
    diagnostics, lifecycle = derive_semantic_hook_evidence(
        process_text, scoped_logs, candidate, module, installed_module)

    positive_request_times = [value for value in request_times if type(value) is int and value > 0]
    if not positive_request_times:
        raise ValueError('QQ capture has no positive request observation time')
    hook_times = []
    for line in scoped:
        header = _THREADTIME_HEADER.fullmatch(line)
        message = header.group(7) or ''
        if message.startswith('('):
            envelope = _LSPOSED_RUNTIME_EVENT.fullmatch(message)
            if not envelope:
                continue
            message = envelope.group(1)
        event = _RUNTIME_EVENT.fullmatch(message)
        if event and event.group(1) == 'hook_installed':
            fields = dict(part.split('=', 1) for part in (event.group(2) or '').split() if '=' in part)
            value = fields.get('elapsedRealtimeNs', '')
            if value.isdigit() and int(value) > 0:
                hook_times.append(int(value))
    if len(hook_times) != 1 or any(hook_times[0] >= value for value in positive_request_times):
        raise ValueError('QQ hook installation must precede every captured request')

    qfix_time = lifecycle['qfixEvidence']['elapsedRealtimeNs']
    native_time = lifecycle['nativeEvidence']['elapsedRealtimeNs']
    lifecycle['timingEvidence'].update(
        qfixReadyBeforeNative=qfix_time < native_time,
        qfixBeforeNative=qfix_time < native_time,
        hookInstalledBeforeRequest=True,
    )
    return diagnostics, lifecycle


def query_target():
    required = {'ro.hardware': 'redroid', 'ro.build.version.sdk': '34', 'ro.product.cpu.abi': 'arm64-v8a',
                'ro.build.fingerprint': FINGERPRINT}
    actual = {key: smoke.adb('shell', 'getprop', key).stdout.decode().strip() for key in required}
    if actual != required:
        raise RuntimeError('ReDroid identity mismatch')
    return actual


@contextmanager
def isolated_tcp(uid):
    query_target()
    created, attached = [], []
    chain = 'IMPAD_QQ_CAPTURE'
    try:
        for tool in ('iptables', 'ip6tables'):
            if smoke.su(f'{tool} -w -S {chain}', check=False).returncode == 0:
                raise RuntimeError('Existing capture isolation chain')
            smoke.su(f'{tool} -w -N {chain}')
            created.append(tool)
            smoke.su(f'{tool} -w -A {chain} -p tcp --sport 5555 -j RETURN')
            smoke.su(f'{tool} -w -A {chain} -m owner --uid-owner {uid} -j RETURN')
            smoke.su(f'{tool} -w -A {chain} -j DROP')
            smoke.su(f'{tool} -w -I OUTPUT 1 ! -o lo -p tcp -j {chain}')
            attached.append(tool)
        yield
    finally:
        for tool in reversed(created):
            if tool in attached:
                smoke.su(f'{tool} -w -D OUTPUT ! -o lo -p tcp -j {chain}')
            smoke.su(f'{tool} -w -F {chain}')
            smoke.su(f'{tool} -w -X {chain}')


def memory_mib(value):
    match = re.fullmatch(r'([0-9.]+)(KiB|MiB|GiB|B) / .+', value)
    if not match:
        raise ValueError('Invalid docker memory sample')
    return float(match[1]) * {'B': 1 / 1048576, 'KiB': 1 / 1024, 'MiB': 1, 'GiB': 1024}[match[2]]


def pipeline_report(report, candidate, module_identity=None, evidence_root=None):
    from scripts.ci.app_policy import policy_for_report
    policy = policy_for_report(candidate, candidate['appId'])
    result = dict(report)
    if isinstance(result.get('baseline'), dict):
        result['baselineEvidence'] = result.pop('baseline')
    result.update(appId=policy.app_id, packageName=policy.package_name,
                  abi=candidate['identity']['abi'], apkSha256=candidate['identity']['apkSha256'],
                  baseline=policy.runtime_baseline, hookIds=list(policy.required_hook_ids),
                  hooks=len(policy.required_hook_ids))
    module_fields = {
        'moduleApkSha256': 'sha256',
        'moduleSourceTreeSha256': 'sourceTreeSha256',
        'ruleCatalogSha256': 'ruleCatalogSha256',
        'recognitionSha256': 'recognitionSha256',
        'dependencyLockSha256': 'dependencyLockSha256',
    }
    module_identity = module_identity if isinstance(module_identity, dict) else {}
    for report_field, module_field in module_fields.items():
        expected = module_identity.get(module_field)
        existing = result.get(report_field)
        if existing is not None and expected is not None and existing != expected:
            result.update(status='RUNTIME_REJECTED',
                          reason=f'Runtime report {report_field} differs from the tested module identity')
        elif expected is not None:
            result[report_field] = expected

    if result.get('status') in {'RUNTIME_SMOKE_VERIFIED', 'RUNTIME_REJECTED'}:
        if any(not isinstance(result.get(field), str)
               or not re.fullmatch(r'[a-f0-9]{64}', result[field])
               for field in module_fields):
            result.update(status='RUNTIME_REJECTED',
                          reason='Verified runtime report lacks complete module source identity')
        elif not isinstance(result.get('evidence'), list):
            try:
                if evidence_root is None:
                    raise ValueError('Runtime evidence root was not provided')
                evidence_root = Path(evidence_root).resolve()
                evidence_directory = Path(result['evidenceDirectory']).resolve()
                evidence_directory.relative_to(evidence_root)
                raw_files = [path for path in sorted(evidence_directory.rglob('*'))
                             if path.is_file() and path.name != 'report.json']
                if not raw_files:
                    raise ValueError('No raw application evidence files were produced')
                result['evidence'] = [
                    {'path': path.relative_to(evidence_root).as_posix(),
                     'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
                    for path in raw_files
                ]
            except (KeyError, OSError, ValueError) as error:
                result.update(status='RUNTIME_REJECTED',
                              reason=f'Runtime report raw evidence is incomplete: {error}')
    if policy.app_id == 'qq':
        result['acceptanceKind'] = 'REQUEST_APP_ID_CONTRAST'
    return result


class AppSmoke:
    def __init__(self, root, probe):
        self.root = root.resolve()
        self.probe = probe
        self.inputs = json.loads((root / 'inputs.json').read_text())
        self.module_hash = hashlib.sha256((root / 'module/app-debug.apk').read_bytes()).hexdigest()
        identity_file = root / 'module-identity.json'
        self.module_identity = json.loads(identity_file.read_text()) if identity_file.is_file() else {}
        os.environ['ANDROID_SERIAL'] = SERIAL
        smoke.ROOT = self.root
        smoke.EVIDENCE = probe.output / 'apps'
        smoke.EVIDENCE.mkdir(parents=True, exist_ok=True)
        smoke.MAGISK = MAGISK
        self.memory_stop = threading.Event()
        self.memory = []
        self.memory_errors = []

    def monitor(self):
        while not self.memory_stop.is_set():
            try:
                result = subprocess.run(['sudo', 'docker', 'stats', '--no-stream', '--format', '{{.MemUsage}}', CONTAINER],
                                        capture_output=True, text=True, timeout=15, check=True)
                value = result.stdout.strip()
                self.memory.append({'time': time.time(), 'memory': value, 'memoryMiB': memory_mib(value)})
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                self.memory_errors.append(str(error))
            self.memory_stop.wait(2)

    def setup(self):
        for module in ('ip_tables', 'iptable_filter', 'ip6_tables', 'ip6table_filter', 'xt_owner', 'xt_tcpudp'):
            self.probe.run(['sudo', 'modprobe', module], 'capture-kernel-' + module)
        query_target()
        # All APKs are checked against preparation evidence before installation.
        for app, profile in self.inputs.items():
            checked_digest(self.root / (app + '.apk'), profile['identity']['apkSha256'])
            resolve_apk_inputs(profile, self.root)
        manager = subprocess.check_output(['7z', 'e', '-so', str(self.probe.output / 'assets/lsposed.zip'), 'manager.apk'], timeout=60)
        (self.root / 'manager.apk').write_bytes(manager)
        for name in ('manager.apk', 'module/app-debug.apk'):
            smoke.adb('install', '-r', str(self.root / name), timeout=120)
        for profile in self.inputs.values():
            install_candidate(smoke, self.root, profile)
        smoke.adb('push', str(self.root / 'ui-hierarchy.jar'), '/data/local/tmp/ui-hierarchy.jar')
        smoke.configure_app_id('qq')
        smoke.adb('shell', 'am', 'start', '-n', 'org.lsposed.manager/.ui.activity.MainActivity')
        time.sleep(3)
        smoke.snapshot('manager-current')
        text = smoke.manager_ocr()
        if smoke.ocr_target(text, 'Activated') is None:
            raise RuntimeError('Official Manager does not show Activated')
        # Navigate with observed accessibility or OCR labels; database reads only confirm UI changes.
        for attempt in range(12):
            smoke.snapshot('manager-current')
            text = smoke.manager_ocr()
            enable = smoke.ocr_target(text, 'Enable module')
            if enable is not None:
                if smoke.manager_module_enabled() is not True:
                    smoke.tap(enable)
                for app in self.inputs:
                    smoke.configure_app_id(app)
                    if runtime_probe.manager_state(smoke)['scopeVerified'] is not True:
                        raise RuntimeError('Missing static app scope')
                return
            module = smoke.ocr_target(text, smoke.MODULE_LABEL)
            if module is not None:
                smoke.tap(module)
            else:
                _, ui = smoke.snapshot('manager-current')
                tab = smoke.unique_node(ui, lambda n: n.get('text') == 'Modules' or n.get('content-desc') == 'Modules')
                if tab is None:
                    tab = smoke.ocr_target(text, 'Modules')
                if tab is None:
                    # The Manager bottom tab is known from the current screenshot, not app screen coordinates.
                    raise RuntimeError('Manager Modules navigation not exposed')
                smoke.tap(tab)
            time.sleep(1)
        raise RuntimeError('Unable to activate module using Manager UI')

    def restart(self):
        smoke.adb('shell', 'am', 'force-stop', smoke.APP_PACKAGE)
        self.probe.run(['sudo', 'docker', 'restart', CONTAINER], 'app-restart')
        self.probe.boot('app-restart')
        query_target()
        smoke.adb('shell', 'am', 'force-stop', smoke.APP_PACKAGE)

    def capture(self, enabled, profile):
        label = 'enabled' if enabled else 'disabled'
        smoke.configure_manager(enabled)
        self.restart()
        if runtime_probe.manager_state(smoke)['moduleEnabled'] is not enabled:
            raise RuntimeError('Module state changed during restart')
        if runtime_probe.installed_hash(smoke, smoke.APP_PACKAGE) != profile['identity']['apkSha256']:
            raise RuntimeError('Installed QQ APK mismatch')
        if runtime_probe.installed_hash(smoke, smoke.MODULE) != self.module_hash:
            raise RuntimeError('Installed module APK mismatch')
        self.capture_installed_splits(profile, label, 'before')
        listing = smoke.adb('shell', 'cmd', 'package', 'list', 'packages', '-U', smoke.APP_PACKAGE).stdout.decode()
        uids = re.findall(r'^package:com\.tencent\.mobileqq uid:([0-9]+)$', listing, re.M)
        if len(uids) != 1 or not 10000 <= int(uids[0]) <= 0x7fffffff:
            raise RuntimeError('QQ UID ambiguous')
        uid = int(uids[0])
        metadata = json.loads((self.root / 'qq-probe/collector.json').read_text())
        binary_hash = checked_digest(self.root / 'qq-probe/collector', metadata['binarySha256'])
        if metadata['observerSha256'] != packet_capture.observer_source_digest() or metadata['ndkVersion'] != prepare_probe.NDK_VERSION or metadata['abi'] != 'arm64':
            raise RuntimeError('Collector metadata mismatch')
        remote = '/data/local/tmp/impad-qq-collector'
        smoke.adb('push', str(self.root / 'qq-probe/collector'), remote)
        smoke.su('chmod 700 ' + remote)
        if smoke.su('sha256sum ' + remote).stdout.decode().split()[0] != binary_hash:
            raise RuntimeError('Installed collector mismatch')
        smoke.su('test ! -e ' + remote + '.pid')
        if smoke.su('pidof impad-qq-collector', check=False).stdout.strip():
            raise RuntimeError('Existing unowned collector')
        smoke.adb('logcat', '-c')
        capture = packet_capture.TcpCapture(uid)
        ready = threading.Event()
        errors = []
        with isolated_tcp(uid):
            with (smoke.EVIDENCE / ('events-' + label + '.jsonl')).open('wb') as events, (smoke.EVIDENCE / ('collector-' + label + '.txt')).open('wb') as stderr:
                command = 'exec ' + remote + ' ' + str(uid) + ' 45 ' + remote + '.pid'
                client = subprocess.Popen([os.environ.get('IMPAD_ADB_BINARY', 'adb'), '-s', SERIAL, 'shell', '-T', MAGISK + ' su -c ' + shlex.quote(command)], stdout=subprocess.PIPE, stderr=stderr)
                def read():
                    try:
                        for line in client.stdout:
                            events.write(line)
                            capture.feed(json.loads(line))
                            if capture.ready:
                                ready.set()
                    except Exception as error:
                        errors.append(str(error))
                reader = threading.Thread(target=read)
                reader.start()
                try:
                    if not ready.wait(10):
                        raise RuntimeError('Collector not ready')
                    smoke.adb('shell', 'am', 'start', '-n', smoke.UI_STRATEGY.launcher_component)
                    client.wait(timeout=55)
                    reader.join(timeout=5)
                    if client.returncode or errors or reader.is_alive():
                        raise RuntimeError(f'Collector rejected {label}: {client.returncode} {errors}')
                finally:
                    if client.poll() is None:
                        client.terminate()
                        client.wait(timeout=5)
                    reader.join(timeout=5)
                    client.stdout.close()
                    smoke.su('rm -f ' + remote + '.pid', check=False)
        result = capture.result()
        (smoke.EVIDENCE / ('capture-' + label + '.json')).write_text(json.dumps(result, indent=2))
        if enabled:
            if not result.get('frames'):
                raise RuntimeError('Enabled QQ request capture has no observed frames')
            if len({owner.get('pid') for owner in result.get('owners', [])
                    if isinstance(owner, dict)}) != 1:
                raise RuntimeError('Enabled QQ capture has ambiguous packet owners')
            capture_qq_process_snapshot(smoke, smoke.EVIDENCE,
                                        {int(owner['pid']) for owner in result.get('owners', [])})
        smoke.save('logcat-' + label + '.txt', smoke.adb('logcat', '-d', '-v', 'threadtime', check=False))
        if not result['captureHealthy'] or not result['ownershipVerified']:
            raise RuntimeError('Capture incomplete or unowned')
        smoke.save('logcat-' + label + '.txt', smoke.adb('logcat', '-d', '-v', 'threadtime'))
        if runtime_probe.manager_state(smoke)['moduleEnabled'] is not enabled or runtime_probe.installed_hash(smoke, smoke.APP_PACKAGE) != profile['identity']['apkSha256'] or runtime_probe.installed_hash(smoke, smoke.MODULE) != self.module_hash:
            raise RuntimeError('App identity or module state changed during capture')
        if smoke.su('sha256sum ' + remote).stdout.decode().split()[0] != binary_hash:
            raise RuntimeError('Collector changed during capture')
        self.capture_installed_splits(profile, label, 'after')
        return dict(result, moduleEnabled=enabled, scopeVerified=True,
                    observerSha256=metadata['observerSha256'], collectorBinarySha256=binary_hash,
                    ndkVersion=metadata['ndkVersion'], apkSha256=profile['identity']['apkSha256'],
                    appUid=uid, moduleApkSha256=self.module_hash, processName='com.tencent.mobileqq:MSF')

    def capture_installed_splits(self, profile, label, stage):
        if len(profile.get('apkFiles', [])) > 1:
            installed = runtime_probe.installed_identity(smoke, smoke.APP_PACKAGE)
            verify_installed_apk_hashes(installed, profile['apkFiles'])
            (smoke.EVIDENCE / f'installed-apks-{label}-{stage}.json').write_text(
                json.dumps(installed, indent=2) + '\n')

    def load_qq_candidate(self):
        candidate_path = self.root / 'candidate-report.json'
        try:
            candidate = json.loads(candidate_path.read_text())
        except FileNotFoundError as error:
            raise ValueError('QQ semantic candidate report is missing') from error
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ValueError('QQ semantic candidate report is unreadable') from error
        if (not isinstance(candidate, dict) or type(candidate.get('schemaVersion')) is not int
                or candidate.get('schemaVersion') != 2):
            raise ValueError('QQ semantic candidate report must use schema version 2')
        if candidate.get('appId') != 'qq':
            raise ValueError('QQ semantic candidate report appId does not match qq')
        profile = self.inputs.get('qq')
        if (not isinstance(profile, dict) or not isinstance(profile.get('identity'), dict)
                or not isinstance(candidate.get('identity'), dict)
                or candidate['identity'] != profile['identity']):
            raise ValueError('QQ semantic candidate identity does not match prepared input')
        return candidate

    def qq(self, profile, candidate):
        smoke.configure_app_id('qq')
        smoke.EVIDENCE = self.probe.output / 'apps' / 'qq'
        smoke.EVIDENCE.mkdir(parents=True, exist_ok=True)
        report = {'appId': 'qq', 'versionName': profile['identity']['versionName'], 'status': 'ACCEPTANCE_UNAVAILABLE',
                  'moduleApkSha256': self.module_hash, 'noAccountAuthentication': True}
        try:
            path = self.root / 'qq.apk'
            checked_digest(path, profile['identity']['apkSha256'])
            smoke.adb('shell', 'am', 'force-stop', smoke.APP_PACKAGE)
            smoke.adb('install', '-r', '-d', str(path), timeout=180)
            smoke.adb('shell', 'pm', 'clear', smoke.APP_PACKAGE)
            if runtime_probe.manager_state(smoke)['scopeVerified'] is not True:
                raise RuntimeError('QQ scope lost during version replacement')
            # Establish first-use login navigation before the isolated fresh launch.
            smoke.mobile_input('login-ready')
            expectation = self.inputs['qq'].get('requestIdentityExpectation', self.inputs['qq'].get('manifestAppIds'))
            report['requestIdentityExpectation'] = expectation
            off = self.capture(False, profile)
            report['disabled'] = off
            on = self.capture(True, profile)
            report['enabled'] = on
            proof = runtime_policy.build_request_identity_proof(expectation, off, on)
            report['requestIdentityEvidence'] = proof
            if not on['frames']:
                raise RuntimeError('Enabled QQ request capture has no observed frames')
            owners = {str(owner['pid']) for owner in on['owners']}
            logs = (smoke.EVIDENCE / 'logcat-enabled.txt').read_text()
            process_text = (smoke.EVIDENCE / 'processes-enabled.txt').read_text()
            installed_module = runtime_probe.installed_identity(smoke, smoke.MODULE)
            report['runtimeDiagnostics'], lifecycle = extract_qq_semantic_lifecycle_evidence(
                logs, {int(owner) for owner in owners},
                [frame.get('firstObservedMonotonicNs') for frame in on['frames']],
                candidate, self.module_identity, process_text, installed_module)
            report['runtimeLifecycleEvidence'] = lifecycle
            report.update(lifecycle)
            if (lifecycle['nativeEvidence']['status'] != 'LOADED'
                    or lifecycle['qfixEvidence']['status'] != 'READY'
                    or not lifecycle['timingEvidence']['qfixReadyBeforeNative']
                    or not lifecycle['timingEvidence']['hookInstalledBeforeRequest']):
                raise RuntimeError('Native, QFix, or hook-before-request evidence is missing')
            dexkit = self.module_identity.get('dexkit', {})
            if (self.module_identity.get('sha256') != self.module_hash
                    or lifecycle['nativeEvidence'].get('apiSha256') != dexkit.get('dexkitAndroidApiSha256')
                    or lifecycle['nativeEvidence'].get('nativeSha256') != dexkit.get('dexkitAndroidArm64NativeSha256')):
                raise RuntimeError('Runtime module or DexKit identity differs from the prepared module')
            if not runtime_policy.validate_request_evidence(proof, profile['identity']['apkSha256'], self.module_hash, expectation):
                report['status'] = 'RUNTIME_REJECTED'
                raise RuntimeError('Trusted request AppID policy rejected pair')
            report['status'] = 'RUNTIME_SMOKE_VERIFIED'
            report.update(moduleApkSha256=self.module_hash,
                          moduleSourceTreeSha256=self.module_identity['sourceTreeSha256'],
                          ruleCatalogSha256=self.module_identity['ruleCatalogSha256'],
                          recognitionSha256=self.module_identity['recognitionSha256'],
                          dependencyLockSha256=self.module_identity['dependencyLockSha256'])
        except Exception as error:
            report['reason'] = f'{type(error).__name__}: {error}'
            try:
                smoke.save('failure-logcat.txt', smoke.adb('logcat', '-d', '-v', 'threadtime', check=False))
                smoke.save('exit-info.txt', smoke.adb('shell', 'dumpsys', 'activity', 'exit-info', smoke.APP_PACKAGE, check=False))
                smoke.snapshot('failure')
            except Exception as diagnostic:
                report['diagnosticError'] = str(diagnostic)
        finally:
            if report.get('status') in {'RUNTIME_SMOKE_VERIFIED', 'RUNTIME_REJECTED'}:
                report['evidence'] = [
                    {'path': path.relative_to(self.probe.output).as_posix(),
                     'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
                    for path in sorted(smoke.EVIDENCE.iterdir())
                    if path.is_file() and path.name != 'report.json'
                ]
            (smoke.EVIDENCE / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
        return report

    def run_candidate(self, candidate):
        from scripts.ci.redroid_ui_smoke import probe as ui_probe
        app = candidate['appId']
        if app == 'qq':
            report = self.qq(self.inputs[app], candidate)
        else:
            self.restart()
            runtime_inputs = {app: dict(self.inputs[app])}
            runtime_inputs[app]['candidateReport'] = candidate
            runtime_inputs[app]['moduleIdentity'] = self.module_identity
            report = ui_probe(app, self.probe.output / 'apps', runtime_inputs, self.module_hash)
        result = pipeline_report(report, candidate, self.module_identity, self.probe.output)
        evidence = self.probe.output / 'evidence'
        evidence.mkdir(exist_ok=True)
        (evidence / 'smoke-report.json').write_text(json.dumps(result, indent=2) + '\n')
        return result


    def execute(self):
        from scripts.ci.redroid_ui_smoke import probe as ui_probe
        thread = threading.Thread(target=self.monitor)
        thread.start()
        reports = []
        failure = None
        qq_candidate = None
        qq_candidate_error = None
        try:
            self.setup()
            for app in ('wechat', 'wecom'):
                if app not in self.inputs:
                    continue
                runtime_inputs = {app: dict(self.inputs[app])}
                runtime_inputs[app]['moduleIdentity'] = self.module_identity
                candidate_path = self.root / 'candidate-report.json'
                if candidate_path.is_file():
                    candidate = json.loads(candidate_path.read_text())
                    if candidate.get('appId') == app:
                        runtime_inputs[app]['candidateReport'] = candidate
                reports.append(ui_probe(app, self.probe.output / 'apps', runtime_inputs, self.module_hash))
            if 'qq' in self.inputs:
                try:
                    qq_candidate = self.load_qq_candidate()
                except ValueError as error:
                    qq_candidate_error = str(error)
                    reports.append({'appId': 'qq', 'status': 'ACCEPTANCE_UNAVAILABLE',
                                    'reason': qq_candidate_error})
                    raise
                reports.append(self.qq(self.inputs['qq'], qq_candidate))
        except Exception as error:
            failure = f'{type(error).__name__}: {error}'
            raise
        finally:
            self.memory_stop.set()
            thread.join(timeout=20)
            candidate_path = self.root / 'candidate-report.json'
            if 'qq' in self.inputs and qq_candidate is None:
                try:
                    qq_candidate = self.load_qq_candidate()
                except ValueError as error:
                    qq_candidate_error = qq_candidate_error or str(error)
            if 'qq' in self.inputs and qq_candidate is None:
                result = {
                    'schemaVersion': 2,
                    'appId': 'qq',
                    'status': 'ACCEPTANCE_UNAVAILABLE',
                    'reason': failure or qq_candidate_error or 'QQ semantic candidate is unavailable',
                    'moduleApkSha256': self.module_hash,
                }
                evidence = self.probe.output / 'evidence'
                evidence.mkdir(exist_ok=True)
                (evidence / 'smoke-report.json').write_text(json.dumps(result, indent=2) + '\n')
            elif candidate_path.is_file():
                candidate = (qq_candidate if 'qq' in self.inputs
                             else json.loads(candidate_path.read_text()))
                result = pipeline_report(reports[0] if len(reports) == 1 and failure is None else {
                    'status': 'ACCEPTANCE_UNAVAILABLE',
                    'reason': failure or 'Candidate runtime did not return one report',
                    'moduleApkSha256': self.module_hash}, candidate,
                    self.module_identity, self.probe.output)
                evidence = self.probe.output / 'evidence'
                evidence.mkdir(exist_ok=True)
                (evidence / 'smoke-report.json').write_text(json.dumps(result, indent=2) + '\n')
            (self.probe.output / 'memory-samples.json').write_text(json.dumps({'samples': self.memory, 'errors': self.memory_errors, 'sampledPeakMiB': max((s['memoryMiB'] for s in self.memory), default=None)}, indent=2))
            (self.probe.output / 'app-reports.json').write_text(json.dumps(reports, indent=2))
        if len(reports) != len(self.inputs) or any(r['status'] != 'RUNTIME_SMOKE_VERIFIED' for r in reports):
            raise RuntimeError('One or more application regression paths failed; see app reports')
