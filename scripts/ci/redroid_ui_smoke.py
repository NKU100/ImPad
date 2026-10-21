"""No-account WeChat and WeCom behavior checks on hosted ReDroid."""
import json
import re
import time
from .redroid_app_smoke import smoke as s, runtime_probe, query_target
from scripts.ci.apps.wechat import runtime_policy as wechat_runtime_policy
from scripts.ci.apps.wecom import runtime_policy

def scoped_logs():
    logs = s.adb('logcat', '-d', '-v', 'threadtime').stdout.decode(errors='replace')
    processes = s.su('ps -A -o PID,NAME').stdout.decode(errors='replace')
    (s.EVIDENCE / 'processes.txt').write_text(processes)
    (s.EVIDENCE / 'logcat.txt').write_text(logs)
    if s.APP_ID in {'wechat', 'wecom'}:
        policy = wechat_runtime_policy if s.APP_ID == 'wechat' else runtime_policy
        selected = policy.scoped_module_log_lines(logs, processes, s.APP_PACKAGE)
        (s.EVIDENCE / 'app-hook-logs.txt').write_text(selected)
        return selected
    pids = {int(fields[0]) for line in processes.splitlines() if len(fields := line.split()) == 2
            and fields[0].isdigit() and (fields[1] == s.APP_PACKAGE or fields[1].startswith(s.APP_PACKAGE + ':'))}
    selected = []
    for line in logs.splitlines():
        match = re.match(r'\S+\s+\S+\s+(\d+)\s+\d+\s+\w\s+(?:ImPad|LSPosedFramework)\s*:', line)
        if match and int(match[1]) in pids:
            selected.append(line)
    (s.EVIDENCE / 'app-hook-logs.txt').write_text('\n'.join(selected) + '\n')
    return '\n'.join(selected)


def probe(app, root, inputs, module_hash):
    s.configure_app_id(app)
    s.EVIDENCE = root / app
    s.EVIDENCE.mkdir(parents=True, exist_ok=True)
    report = {'appId': app, 'status': 'ACCEPTANCE_UNAVAILABLE', 'evidenceDirectory': str(s.EVIDENCE),
              'input': inputs[app], 'moduleApkSha256': module_hash, 'noAccountAuthentication': True}
    manager_states = {}
    strict_gate = app in {'wechat', 'wecom'}
    policy_runtime = wechat_runtime_policy if app == 'wechat' else runtime_policy

    def save_manager_state(label):
        if not strict_gate:
            return runtime_probe.manager_state(s)
        raw = runtime_probe.manager_state_evidence(s)
        derived = policy_runtime.derive_manager_state(raw, s.MODULE, s.APP_PACKAGE)
        (s.EVIDENCE / f'manager-state-{label}.json').write_text(json.dumps(raw, indent=2) + '\n')
        manager_states[label] = derived
        report['managerStates'] = dict(manager_states)
        return derived

    try:
        report['target'] = query_target()
        for package in ('com.tencent.mobileqq', 'com.tencent.mm', 'com.tencent.wework'):
            s.adb('shell', 'am', 'force-stop', package)
        installed = None
        if strict_gate:
            installed = {
                'schemaVersion': 1,
                'app': runtime_probe.installed_identity(s, s.APP_PACKAGE),
                'module': runtime_probe.installed_identity(s, s.MODULE),
            }
            if installed['app']['baseApkSha256'] != inputs[app]['identity']['apkSha256']:
                raise RuntimeError('Installed app APK differs from the verified input')
            if installed['module']['baseApkSha256'] != module_hash:
                raise RuntimeError('Installed ImPad differs from the verified module APK')
            (s.EVIDENCE / 'installed-identities.json').write_text(json.dumps(installed, indent=2) + '\n')
            report['installedIdentities'] = installed
        elif (runtime_probe.installed_hash(s, s.APP_PACKAGE) != inputs[app]['identity']['apkSha256']
              or runtime_probe.installed_hash(s, s.MODULE) != module_hash):
            raise RuntimeError('Installed app or ImPad differs from the verified input')
        report['initialManagerState'] = save_manager_state('initial')
        s.configure_manager(False)
        s.adb('shell', 'pm', 'clear', s.APP_PACKAGE)
        s.adb('logcat', '-c')
        off = s.mobile_input('module-off-login')
        disabled_state = save_manager_state('disabled')
        if disabled_state['moduleEnabled'] is not False:
            raise RuntimeError('Module-off state changed')
        s.configure_manager(True)
        if strict_gate:
            enabled_state = save_manager_state('enabled')
            if enabled_state['moduleEnabled'] is not True:
                raise RuntimeError('Module-on state changed')
        s.adb('shell', 'pm', 'clear', s.APP_PACKAGE)
        s.adb('logcat', '-c')
        on = s.mobile_input('module-on-login')
        if strict_gate and ('candidateReport' not in inputs[app] or 'moduleIdentity' not in inputs[app]):
            raise RuntimeError('Rule candidate plan or prepared module identity is missing')
        logs = scoped_logs()
        choice = s.unique_node(on, lambda n: n.get('package') == s.APP_PACKAGE
                               and s.is_tablet_entry(n.get('text', '')))
        if choice is None:
            report['status'] = 'RUNTIME_REJECTED'
            raise RuntimeError('Module-on login page has no tablet entry')
        s.tap(choice)
        ready = False
        for attempt in range(15):
            activity, ui = s.snapshot('tablet-qr-current')
            if s.qr_page_ready(activity, ui):
                time.sleep(3)
                activity, ui = s.snapshot('tablet-qr-confirmed')
                if s.qr_page_ready(activity, ui):
                    ready = True
                    break
            elif app == 'wecom':
                agreement = s.unique_node(ui, lambda n: s.UI_STRATEGY.is_login_navigation_label(n.get('text', '')))
                if agreement is not None:
                    s.tap(agreement)
            time.sleep(2)
        if not ready:
            report['status'] = 'RUNTIME_REJECTED'
            raise RuntimeError('Tablet entry did not reach the expected stable QR page')
        logs = scoped_logs()
        processes = (s.EVIDENCE / 'processes.txt').read_text()
        candidate = inputs[app]['candidateReport']
        module_identity = inputs[app]['moduleIdentity']
        if strict_gate:
            candidate = inputs[app]['candidateReport']
            module_identity = inputs[app]['moduleIdentity']
            report['runtimeDiagnostics'], report['runtimeLifecycleEvidence'] = policy_runtime.derive_hook_evidence(
                processes, logs, candidate, module_identity, installed['module'])
            samples = {
                'baseline': {'activity': (s.EVIDENCE / 'module-off-login-activity.txt').read_text(),
                             'hierarchy': (s.EVIDENCE / 'module-off-login.xml').read_text()},
                'enabled': {'activity': (s.EVIDENCE / 'module-on-login-activity.txt').read_text(),
                            'hierarchy': (s.EVIDENCE / 'module-on-login.xml').read_text()},
                'qrCurrent': {'activity': (s.EVIDENCE / 'tablet-qr-current-activity.txt').read_text(),
                              'hierarchy': (s.EVIDENCE / 'tablet-qr-current.xml').read_text()},
                'qrConfirmed': {'activity': (s.EVIDENCE / 'tablet-qr-confirmed-activity.txt').read_text(),
                                'hierarchy': (s.EVIDENCE / 'tablet-qr-confirmed.xml').read_text()},
            }
            ui_evidence = policy_runtime.derive_ui_evidence(
                samples, s.APP_PACKAGE, s.APP_POLICY.runtime_qr_page)
            if ui_evidence['baseline']['tabletEntry'] or not ui_evidence['enabled']['tabletEntry']:
                raise RuntimeError('Tablet entry did not change only with the module enabled')
            report['baseline'] = dict(manager_states['disabled'], **ui_evidence['baseline'])
            report['enabled'] = dict(manager_states['enabled'], **ui_evidence['enabled'])
            report['qrPage'] = ui_evidence['qrPage']
            report['qrStable'] = ui_evidence['qrStable']
        else:
            report['runtimeDiagnostics'] = s.hook_diagnostics(logs)
            report['baseline'] = {'moduleEnabled': False, 'loginPageReady': True,
                                  'tabletEntry': any(s.is_tablet_entry(n.get('text', '')) for n in s.nodes(off))}
            report['enabled'] = {'moduleEnabled': True, 'loginPageReady': True,
                                 'tabletEntry': any(s.is_tablet_entry(n.get('text', '')) for n in s.nodes(on))}
            if report['baseline']['tabletEntry']:
                raise RuntimeError('Baseline already has a tablet entry')
        diagnostics = report['runtimeDiagnostics']
        expected = 'INSTALLED_' + str(len(s.APP_POLICY.required_hook_ids))
        if diagnostics['compatibility'] != 'COMPATIBLE' or diagnostics['hookInstallation'] != expected:
            report['status'] = 'RUNTIME_REJECTED'
            raise RuntimeError('QR page lacks compatible app-process Hook evidence')
        report.update(status='RUNTIME_SMOKE_VERIFIED', qrPage=s.APP_POLICY.runtime_qr_page)
    except Exception as error:
        report['reason'] = type(error).__name__ + ': ' + str(error)
        try:
            s.snapshot('failure')
            report['runtimeDiagnostics'] = s.hook_diagnostics(scoped_logs())
        except Exception as diagnostic:
            report['diagnosticError'] = str(diagnostic)
    finally:
        try:
            s.configure_manager(True)
            final_state = save_manager_state('final')
            report['finalManagerState'] = final_state
        except Exception as error:
            report['status'] = 'ACCEPTANCE_UNAVAILABLE'
            report['restoreError'] = str(error)
        (s.EVIDENCE / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, sort_keys=True), flush=True)
    return report
