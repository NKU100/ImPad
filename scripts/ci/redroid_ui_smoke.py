"""No-account WeChat and WeCom behavior checks on hosted ReDroid."""
import json
import re
import time
from .redroid_app_smoke import smoke as s, runtime_probe, query_target

def scoped_logs():
    logs = s.adb('logcat', '-d', '-v', 'threadtime').stdout.decode(errors='replace')
    processes = s.su('ps -A -o PID,NAME').stdout.decode(errors='replace')
    (s.EVIDENCE / 'processes.txt').write_text(processes)
    (s.EVIDENCE / 'logcat.txt').write_text(logs)
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
    try:
        report['target'] = query_target()
        for package in ('com.tencent.mobileqq', 'com.tencent.mm', 'com.tencent.wework'):
            s.adb('shell', 'am', 'force-stop', package)
        if runtime_probe.installed_hash(s, s.APP_PACKAGE) != inputs[app]['identity']['apkSha256']:
            raise RuntimeError('Installed app APK differs from the verified input')
        if runtime_probe.installed_hash(s, s.MODULE) != module_hash:
            raise RuntimeError('Installed ImPad differs from the QQ test module')
        report['initialManagerState'] = runtime_probe.manager_state(s)
        s.configure_manager(False)
        s.adb('shell', 'pm', 'clear', s.APP_PACKAGE)
        s.adb('logcat', '-c')
        off = s.mobile_input('module-off-login')
        if runtime_probe.manager_state(s)['moduleEnabled'] is not False:
            raise RuntimeError('Module-off state changed')
        report['baseline'] = {'moduleEnabled': False, 'loginPageReady': True,
                              'tabletEntry': any(s.is_tablet_entry(n.get('text', '')) for n in s.nodes(off))}
        if report['baseline']['tabletEntry']:
            raise RuntimeError('Baseline already has a tablet entry')
        s.configure_manager(True)
        s.adb('shell', 'pm', 'clear', s.APP_PACKAGE)
        s.adb('logcat', '-c')
        on = s.mobile_input('module-on-login')
        report['enabled'] = {'moduleEnabled': True, 'loginPageReady': True,
                             'tabletEntry': any(s.is_tablet_entry(n.get('text', '')) for n in s.nodes(on))}
        logs = scoped_logs()
        report['runtimeDiagnostics'] = s.hook_diagnostics(logs)
        choice = s.unique_node(on, lambda n: s.is_tablet_entry(n.get('text', '')))
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
        report['runtimeDiagnostics'] = s.hook_diagnostics(scoped_logs())
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
            report['finalManagerState'] = runtime_probe.manager_state(s)
        except Exception as error:
            report['status'] = 'ACCEPTANCE_UNAVAILABLE'
            report['restoreError'] = str(error)
        (s.EVIDENCE / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, sort_keys=True), flush=True)
    return report
