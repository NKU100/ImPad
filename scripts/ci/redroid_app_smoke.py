"""Run strict application acceptance in a disposable hosted ARM64 ReDroid."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import threading
import time
import zipfile

from scripts.ci import runtime_smoke as smoke
from scripts.ci.apps.qq import packet_capture, prepare_probe, request_protocol, runtime_policy, runtime_probe
from scripts.ci.redroid_environment_probe import Probe, SERIAL, CONTAINER, MAGISK, checked_digest

FINGERPRINT = 'redroid/redroid_arm64_only/redroid_arm64_only:14/UD2A.240505.001.W1/eng.frank.20240527.152423:userdebug/test-keys'


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


class AppSmoke:
    def __init__(self, root, probe):
        self.root = root.resolve()
        self.probe = probe
        self.inputs = json.loads((root / 'inputs.json').read_text())
        self.module_hash = hashlib.sha256((root / 'module/app-debug.apk').read_bytes()).hexdigest()
        os.environ['ANDROID_SERIAL'] = SERIAL
        smoke.ROOT = self.root
        smoke.EVIDENCE = probe.output / 'apps'
        smoke.EVIDENCE.mkdir(exist_ok=True)
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
        query_target()
        # All APKs are checked against preparation evidence before installation.
        for app, profile in self.inputs.items():
            checked_digest(self.root / (app + '.apk'), profile['identity']['apkSha256'])
        with zipfile.ZipFile(self.probe.output / 'assets/lsposed.zip') as archive:
            (self.root / 'manager.apk').write_bytes(archive.read('manager.apk'))
        for name in ('manager.apk', 'module/app-debug.apk'):
            smoke.adb('install', '-r', str(self.root / name), timeout=120)
        for app in ('qq', 'wechat', 'wecom'):
            smoke.adb('install', '-r', str(self.root / (app + '.apk')), timeout=180)
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
                for app in ('qq', 'wechat', 'wecom'):
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
                client = subprocess.Popen(['adb', '-s', SERIAL, 'shell', '-T', MAGISK + ' su -c ' + shlex.quote(command)], stdout=subprocess.PIPE, stderr=stderr)
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
        smoke.save('logcat-' + label + '.txt', smoke.adb('logcat', '-d', '-v', 'threadtime', check=False))
        if not result['captureHealthy'] or not result['ownershipVerified']:
            raise RuntimeError('Capture incomplete or unowned')
        smoke.save('logcat-' + label + '.txt', smoke.adb('logcat', '-d', '-v', 'threadtime'))
        if runtime_probe.manager_state(smoke)['moduleEnabled'] is not enabled or runtime_probe.installed_hash(smoke, smoke.APP_PACKAGE) != profile['identity']['apkSha256'] or runtime_probe.installed_hash(smoke, smoke.MODULE) != self.module_hash:
            raise RuntimeError('App identity or module state changed during capture')
        if smoke.su('sha256sum ' + remote).stdout.decode().split()[0] != binary_hash:
            raise RuntimeError('Collector changed during capture')
        return dict(result, moduleEnabled=enabled, scopeVerified=True,
                    observerSha256=metadata['observerSha256'], collectorBinarySha256=binary_hash,
                    ndkVersion=metadata['ndkVersion'], apkSha256=profile['identity']['apkSha256'],
                    appUid=uid, moduleApkSha256=self.module_hash, processName='com.tencent.mobileqq:MSF')

    def qq(self, profile, name):
        smoke.configure_app_id('qq')
        smoke.EVIDENCE = self.probe.output / 'apps' / name
        smoke.EVIDENCE.mkdir(exist_ok=True)
        report = {'appId': 'qq', 'versionName': profile['identity']['versionName'], 'status': 'ACCEPTANCE_UNAVAILABLE',
                  'moduleApkSha256': self.module_hash, 'noAccountAuthentication': True}
        try:
            path = self.root / (name + '.apk')
            checked_digest(path, profile['identity']['apkSha256'])
            smoke.adb('uninstall', smoke.APP_PACKAGE)
            smoke.adb('install', str(path), timeout=180)
            # Establish first-use login navigation before the isolated fresh launch.
            smoke.mobile_input('login-ready')
            expectation = self.inputs[name]['manifestAppIds']
            off = self.capture(False, profile)
            report['disabled'] = off
            on = self.capture(True, profile)
            report['enabled'] = on
            proof = {'contract': request_protocol.CONTRACT, **expectation, 'disabled': off, 'enabled': on}
            report['requestIdentityEvidence'] = proof
            owners = {str(owner['pid']) for owner in on['owners']}
            logs = (smoke.EVIDENCE / 'logcat-enabled.txt').read_text().splitlines()
            compatible, installed = set(), set()
            for line in logs:
                fields = line.split()
                if len(fields) > 5 and fields[2] in owners and fields[5].rstrip(':') in ('ImPad', 'LSPosedFramework'):
                    if 'status=COMPATIBLE' in line:
                        compatible.add(fields[2])
                    if 'installed 1 QQ hooks' in line:
                        installed.add(fields[2])
            if not compatible & installed:
                raise RuntimeError('No fresh compatible and installed hook in captured MSF owner')
            if not runtime_policy.validate_request_evidence(proof, profile['identity']['apkSha256'], self.module_hash, expectation):
                report['status'] = 'RUNTIME_REJECTED'
                raise RuntimeError('Trusted request AppID policy rejected pair')
            report['status'] = 'RUNTIME_SMOKE_VERIFIED'
        except Exception as error:
            report['reason'] = f'{type(error).__name__}: {error}'
            try:
                smoke.save('failure-logcat.txt', smoke.adb('logcat', '-d', '-v', 'threadtime', check=False))
                smoke.save('exit-info.txt', smoke.adb('shell', 'dumpsys', 'activity', 'exit-info', smoke.APP_PACKAGE, check=False))
                smoke.snapshot('failure')
            except Exception as diagnostic:
                report['diagnosticError'] = str(diagnostic)
        finally:
            (smoke.EVIDENCE / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
        return report

    def execute(self):
        from scripts.ci.redroid_ui_smoke import probe as ui_probe
        thread = threading.Thread(target=self.monitor)
        thread.start()
        reports = []
        try:
            self.setup()
            for app in ('wechat', 'wecom'):
                reports.append(ui_probe(app, self.probe.output / 'apps', self.inputs, self.module_hash))
            for name in ('qq-old', 'qq'):
                reports.append(self.qq(self.inputs[name], name))
        finally:
            self.memory_stop.set()
            thread.join(timeout=20)
            (self.probe.output / 'memory-samples.json').write_text(json.dumps({'samples': self.memory, 'errors': self.memory_errors, 'sampledPeakMiB': max((s['memoryMiB'] for s in self.memory), default=None)}, indent=2))
            (self.probe.output / 'app-reports.json').write_text(json.dumps(reports, indent=2))
        if len(reports) != 4 or any(r['status'] != 'RUNTIME_SMOKE_VERIFIED' for r in reports):
            raise RuntimeError('One or more application regression paths failed; see app reports')
