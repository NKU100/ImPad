"""Verify acceptance records and register only fully validated runtime evidence."""
import argparse
import copy
import hashlib
import json
import os
import subprocess
from pathlib import Path
import re

from scripts.ci.app_policy import get_policy, policy_for_report
from scripts.ci.discover_latest_wechat import validate_official_apk_url


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _verify_regression_snapshot(rows, snapshot, app_id, initial_migration,
                                catalog, repository):
    from scripts.ci.acceptance import (
        AcceptanceSnapshot, select_representatives, validate_acceptance_snapshot,
    )

    if (not isinstance(snapshot, AcceptanceSnapshot)
            or snapshot.initial_migration is not bool(initial_migration)):
        raise ValueError('Regression validation requires its verified acceptance snapshot')
    records = validate_acceptance_snapshot(snapshot, app_id, initial_migration,
                                           catalog=catalog, repository=repository)
    binding = {'path': str(snapshot.path), 'sha256': snapshot.sha256,
               'recordIds': [record.get('recordId') for record in records],
               'initialMigration': bool(initial_migration)}
    representatives = select_representatives(records)
    if not representatives:
        if not initial_migration:
            raise ValueError('NO_HISTORICAL_SAMPLES requires an explicit initial migration')
        if (len(rows) != 1 or not isinstance(rows[0], dict)
                or rows[0].get('appId') != app_id
                or rows[0].get('status') != 'NO_HISTORICAL_SAMPLES'
                or rows[0].get('acceptanceSnapshotSha256') != snapshot.sha256
                or rows[0].get('initialMigration') is not True):
            raise ValueError('Empty acceptance history requires exactly one bound NO_HISTORICAL_SAMPLES row')
        return 'NO_HISTORICAL_SAMPLES', binding

    if len(rows) != len(representatives):
        raise ValueError('Regression rows do not exactly match the acceptance representatives')
    expected = {}
    for record in representatives:
        record_id = record.get('recordId')
        if not isinstance(record_id, str) or not record_id or record_id in expected:
            raise ValueError('Acceptance representatives contain a missing or duplicate recordId')
        expected[record_id] = record

    seen = set()
    statuses = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('Regression representative row is malformed')
        record_id = row.get('recordId')
        if (record_id not in expected or record_id in seen
                or row.get('appId') != app_id
                or row.get('identity') != expected[record_id].get('identity')
                or row.get('expectedPlan') != expected[record_id].get('expectedPlan')):
            raise ValueError('Regression rows differ from the fixed acceptance representative set')
        if (row.get('acceptanceSnapshotSha256') != snapshot.sha256
                or row.get('initialMigration') is not bool(initial_migration)):
            raise ValueError('Regression rows are not bound to the verified acceptance snapshot')
        if row.get('status') not in {'PASSED', 'FAILED'}:
            raise ValueError('Historical acceptance representatives cannot be reported as NO_HISTORICAL_SAMPLES')
        seen.add(record_id)
        statuses.append(row['status'])
    if seen != set(expected):
        raise ValueError('Regression rows omit a fixed acceptance representative')
    return ('PASSED' if all(status == 'PASSED' for status in statuses) else 'FAILED'), binding


def _resolve_child(root, value, label):
    root = Path(root).resolve()
    path = Path(value)
    if path.is_absolute():
        resolved = path.resolve()
    else:
        resolved = (root / path).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise ValueError(f'{label} path escapes its evidence root') from error
    if not resolved.is_file():
        raise FileNotFoundError(f'{label} file is missing: {resolved}')
    return resolved


def _read_json(path, label):
    try:
        value = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f'{label} JSON is unreadable') from error
    if not isinstance(value, (dict, list)):
        raise ValueError(f'{label} JSON has an invalid root value')
    return value


def _validate_qq_raw_evidence(smoke, evidence_root, candidate, module, *, allow_app_id_mismatch=False):
    from scripts.ci.apps.qq import packet_capture, request_protocol, runtime_policy
    from scripts.ci.redroid_app_smoke import extract_qq_lifecycle_evidence, qq_compatible_hook_installed

    entries = smoke.get('evidence')
    if not isinstance(entries, list) or not entries:
        raise ValueError('Runtime report has no raw evidence references')
    files = {}
    for item in entries:
        if (not isinstance(item, dict) or not isinstance(item.get('path'), str)
                or not re.fullmatch(r'[a-f0-9]{64}', item.get('sha256', '').lower())):
            raise ValueError('Runtime evidence reference is malformed')
        path = _resolve_child(evidence_root, item['path'], 'Runtime evidence')
        digest = _sha256(path)
        if digest != item['sha256'].lower():
            raise ValueError(f'Runtime evidence hash changed: {item["path"]}')
        key = Path(item['path']).as_posix()
        if key in files:
            raise ValueError('Runtime evidence contains duplicate file references')
        files[key] = path

    expectation = candidate.get('requestIdentityExpectation')
    if not isinstance(expectation, dict) or expectation.get('contract') != request_protocol.CONTRACT:
        raise ValueError('Candidate request identity expectation is missing')
    apk_path = _resolve_child(Path(candidate['_root']), candidate['apkFiles'][0]['path'], 'Candidate base APK')
    from scripts.ci.apps.qq.runtime_probe import manifest_expectation
    actual_expectation = manifest_expectation(apk_path, candidate['identity']['apkSha256'])
    if actual_expectation != expectation:
        raise ValueError('Candidate request expectation does not match the base APK manifest')

    captures = {}
    for label in ('disabled', 'enabled'):
        raw_path = files.get(f'apps/qq/events-{label}.jsonl')
        summary_path = files.get(f'apps/qq/capture-{label}.json')
        report_capture = smoke.get(label)
        if raw_path is None or summary_path is None or not isinstance(report_capture, dict):
            raise ValueError(f'QQ {label} raw packet capture is incomplete')
        try:
            capture = packet_capture.TcpCapture(report_capture['appUid'])
            for line in raw_path.read_text().splitlines():
                if line:
                    capture.feed(json.loads(line))
            replayed = capture.result()
            saved = json.loads(summary_path.read_text())
        except (KeyError, OSError, json.JSONDecodeError, TypeError) as error:
            raise ValueError(f'QQ {label} packet capture cannot be parsed') from error
        if replayed != saved or any(report_capture.get(key) != value for key, value in replayed.items()):
            raise ValueError(f'QQ {label} report does not match its raw packet capture')
        captures[label] = report_capture

    proof = runtime_policy.build_request_identity_proof(
        actual_expectation, captures['disabled'], captures['enabled'])
    if smoke.get('requestIdentityEvidence') != proof:
        raise ValueError('QQ request identity proof differs from the raw capture reports')
    if not runtime_policy.validate_request_evidence(
            proof, candidate['identity']['apkSha256'], module['sha256'], expectation,
            allow_app_id_mismatch=allow_app_id_mismatch):
        raise ValueError('QQ request identity evidence failed the shared policy')

    logs = files.get('apps/qq/logcat-enabled.txt')
    if logs is None:
        raise ValueError('QQ enabled logcat evidence is missing')
    owners = {item['pid'] for item in captures['enabled'].get('owners', [])}
    log_text = logs.read_text()
    if not qq_compatible_hook_installed(log_text, owners):
        raise ValueError('QQ raw logcat does not show a compatible installed hook in one MSF owner')
    lifecycle = extract_qq_lifecycle_evidence(
        log_text, owners,
        [frame.get('firstObservedMonotonicNs') for frame in captures['enabled'].get('frames', [])],
    )
    if any(smoke.get(key) != value for key, value in lifecycle.items()):
        raise ValueError('QQ lifecycle summary does not match the captured logcat')
    dexkit = module.get('dexkit', {})
    if (lifecycle['nativeEvidence'].get('status') != 'LOADED'
            or lifecycle['nativeEvidence'].get('apiSha256') != dexkit.get('dexkitAndroidApiSha256')
            or lifecycle['nativeEvidence'].get('nativeSha256') != dexkit.get('dexkitAndroidArm64NativeSha256')
            or lifecycle['qfixEvidence'].get('status') != 'READY'
            or lifecycle['qfixEvidence'].get('contextPackage') != 'com.tencent.mobileqq'
            or lifecycle['timingEvidence'] != {'qfixReadyBeforeNative': True,
                                               'hookInstalledBeforeRequest': True}):
        raise ValueError('QQ lifecycle evidence does not match the tested module identity')
    return files


def _validate_wecom_raw_evidence(smoke, evidence_root, candidate, module):
    from scripts.ci.apps.wecom import runtime_policy

    entries = smoke.get('evidence')
    if not isinstance(entries, list) or not entries:
        raise ValueError('WeCom runtime report has no raw evidence references')
    files = {}
    for item in entries:
        if (not isinstance(item, dict) or not isinstance(item.get('path'), str)
                or not re.fullmatch(r'[a-f0-9]{64}', item.get('sha256', '').lower())):
            raise ValueError('WeCom runtime evidence reference is malformed')
        path = _resolve_child(evidence_root, item['path'], 'WeCom runtime evidence')
        if _sha256(path) != item['sha256'].lower():
            raise ValueError(f'WeCom runtime evidence hash changed: {item["path"]}')
        key = Path(item['path']).as_posix()
        if key in files:
            raise ValueError('WeCom runtime evidence contains duplicate references')
        files[key] = path

    required = {
        'apps/wecom/module-off-login.xml',
        'apps/wecom/module-off-login-activity.txt',
        'apps/wecom/module-on-login.xml',
        'apps/wecom/module-on-login-activity.txt',
        'apps/wecom/tablet-qr-current.xml',
        'apps/wecom/tablet-qr-current-activity.txt',
        'apps/wecom/tablet-qr-confirmed.xml',
        'apps/wecom/tablet-qr-confirmed-activity.txt',
        'apps/wecom/logcat.txt',
        'apps/wecom/processes.txt',
        'apps/wecom/app-hook-logs.txt',
        'apps/wecom/installed-identities.json',
        'apps/wecom/manager-state-initial.json',
        'apps/wecom/manager-state-disabled.json',
        'apps/wecom/manager-state-enabled.json',
        'apps/wecom/manager-state-final.json',
    }
    if required - files.keys():
        raise ValueError('WeCom runtime evidence is missing required UI, process, module or manager snapshots')

    package = candidate.get('identity', {}).get('packageName')
    policy = get_policy('wecom')
    if package != policy.package_name:
        raise ValueError('WeCom candidate package differs from policy')
    read_text = lambda name: files[f'apps/wecom/{name}'].read_text()
    samples = {
        'baseline': {'activity': read_text('module-off-login-activity.txt'),
                     'hierarchy': read_text('module-off-login.xml')},
        'enabled': {'activity': read_text('module-on-login-activity.txt'),
                    'hierarchy': read_text('module-on-login.xml')},
        'qrCurrent': {'activity': read_text('tablet-qr-current-activity.txt'),
                      'hierarchy': read_text('tablet-qr-current.xml')},
        'qrConfirmed': {'activity': read_text('tablet-qr-confirmed-activity.txt'),
                        'hierarchy': read_text('tablet-qr-confirmed.xml')},
    }
    ui = runtime_policy.derive_ui_evidence(samples, package, policy.runtime_qr_page)
    baseline = smoke.get('baselineEvidence')
    enabled = smoke.get('enabled')
    if (not isinstance(baseline, dict) or not isinstance(enabled, dict)
            or baseline.get('loginPageReady') is not ui['baseline']['loginPageReady']
            or baseline.get('tabletEntry') is not ui['baseline']['tabletEntry']
            or ui['baseline']['tabletEntry'] is not False
            or baseline.get('moduleEnabled') is not False
            or enabled.get('loginPageReady') is not ui['enabled']['loginPageReady']
            or enabled.get('tabletEntry') is not ui['enabled']['tabletEntry']
            or ui['enabled']['tabletEntry'] is not True
            or enabled.get('moduleEnabled') is not True
            or smoke.get('qrPage') != ui['qrPage'] or smoke.get('qrStable') is not True):
        raise ValueError('WeCom runtime UI summary differs from the captured hierarchy and activity files')

    manager_states = {}
    for stage in ('initial', 'disabled', 'enabled', 'final'):
        raw = _read_json(files[f'apps/wecom/manager-state-{stage}.json'], 'WeCom manager state')
        manager_states[stage] = runtime_policy.derive_manager_state(raw, 'io.github.nku100.impad', package)
    if (smoke.get('managerStates') != manager_states
            or smoke.get('initialManagerState') != manager_states['initial']
            or smoke.get('baselineEvidence', {}).get('scopeVerified') is not True
            or smoke.get('enabled', {}).get('scopeVerified') is not True
            or smoke.get('baselineEvidence', {}).get('moduleEnabled') != manager_states['disabled']['moduleEnabled']
            or smoke.get('enabled', {}).get('moduleEnabled') != manager_states['enabled']['moduleEnabled']
            or smoke.get('finalManagerState') != manager_states['final']
            or manager_states['disabled']['moduleEnabled'] is not False
            or manager_states['enabled']['moduleEnabled'] is not True
            or manager_states['final']['moduleEnabled'] is not True):
        raise ValueError('WeCom manager state summary differs from the captured module/scope query rows')

    installed = _read_json(files['apps/wecom/installed-identities.json'], 'WeCom installed APK identity')
    runtime_policy.derive_installed_identities(
        installed, package, candidate.get('identity', {}).get('apkSha256'),
        'io.github.nku100.impad', module.get('sha256'))
    if smoke.get('installedIdentities') != installed:
        raise ValueError('WeCom installed APK identity summary differs from its captured raw file')
    if smoke.get('moduleApkSha256') != installed['module']['baseApkSha256']:
        raise ValueError('WeCom installed module APK digest differs from the tested module')

    processes = read_text('processes.txt')
    logcat = read_text('logcat.txt')
    scoped = runtime_policy.scoped_module_log_lines(logcat, processes, package)
    if read_text('app-hook-logs.txt') != scoped:
        raise ValueError('WeCom scoped module logs differ from raw logcat and process ownership')
    diagnostics, lifecycle = runtime_policy.derive_hook_evidence(
        processes, scoped, candidate, module, installed['module'])
    if (smoke.get('runtimeDiagnostics') != diagnostics
            or smoke.get('runtimeLifecycleEvidence') != lifecycle):
        raise ValueError('WeCom hook, Tinker or native summary differs from the captured runtime logs')
    if (diagnostics['compatibility'] != 'COMPATIBLE'
            or diagnostics['hookInstallation'] != f'INSTALLED_{len(policy.required_hook_ids)}'
            or lifecycle['hookCount'] != len(policy.required_hook_ids)
            or lifecycle['tinkerEvidence']['status'] != 'READY'
            or lifecycle['nativeEvidence']['status'] != 'LOADED'):
        raise ValueError('WeCom raw runtime lifecycle evidence does not satisfy the required hook gate')
    return files


def _validate_wechat_raw_evidence(smoke, evidence_root, candidate, module):
    from scripts.ci.apps.wechat import runtime_policy

    entries = smoke.get('evidence')
    if not isinstance(entries, list) or not entries:
        raise ValueError('WeChat runtime report has no raw evidence references')
    files = {}
    for item in entries:
        if (not isinstance(item, dict) or not isinstance(item.get('path'), str)
                or not isinstance(item.get('sha256'), str)
                or not re.fullmatch(r'[a-f0-9]{64}', item['sha256'].lower())):
            raise ValueError('WeChat runtime evidence reference is malformed')
        path = _resolve_child(evidence_root, item['path'], 'WeChat runtime evidence')
        if _sha256(path) != item['sha256'].lower():
            raise ValueError(f'WeChat runtime evidence hash changed: {item["path"]}')
        key = Path(item['path']).as_posix()
        if key in files:
            raise ValueError('WeChat runtime evidence contains duplicate references')
        files[key] = path

    required = {
        'apps/wechat/module-off-login.xml',
        'apps/wechat/module-off-login-activity.txt',
        'apps/wechat/module-on-login.xml',
        'apps/wechat/module-on-login-activity.txt',
        'apps/wechat/tablet-qr-current.xml',
        'apps/wechat/tablet-qr-current-activity.txt',
        'apps/wechat/tablet-qr-confirmed.xml',
        'apps/wechat/tablet-qr-confirmed-activity.txt',
        'apps/wechat/logcat.txt',
        'apps/wechat/processes.txt',
        'apps/wechat/app-hook-logs.txt',
        'apps/wechat/installed-identities.json',
        'apps/wechat/manager-state-initial.json',
        'apps/wechat/manager-state-disabled.json',
        'apps/wechat/manager-state-enabled.json',
        'apps/wechat/manager-state-final.json',
    }
    if required - files.keys():
        raise ValueError('WeChat runtime evidence is missing required UI, process, module or manager snapshots')

    package = candidate.get('identity', {}).get('packageName')
    policy = get_policy('wechat')
    if package != policy.package_name:
        raise ValueError('WeChat candidate package differs from policy')
    read_text = lambda name: files[f'apps/wechat/{name}'].read_text()
    samples = {
        'baseline': {'activity': read_text('module-off-login-activity.txt'),
                     'hierarchy': read_text('module-off-login.xml')},
        'enabled': {'activity': read_text('module-on-login-activity.txt'),
                    'hierarchy': read_text('module-on-login.xml')},
        'qrCurrent': {'activity': read_text('tablet-qr-current-activity.txt'),
                      'hierarchy': read_text('tablet-qr-current.xml')},
        'qrConfirmed': {'activity': read_text('tablet-qr-confirmed-activity.txt'),
                        'hierarchy': read_text('tablet-qr-confirmed.xml')},
    }
    ui = runtime_policy.derive_ui_evidence(samples, package, policy.runtime_qr_page)
    baseline, enabled = smoke.get('baselineEvidence'), smoke.get('enabled')
    if (not isinstance(baseline, dict) or not isinstance(enabled, dict)
            or baseline.get('loginPageReady') is not ui['baseline']['loginPageReady']
            or baseline.get('tabletEntry') is not ui['baseline']['tabletEntry']
            or ui['baseline']['tabletEntry'] is not False or baseline.get('moduleEnabled') is not False
            or enabled.get('loginPageReady') is not ui['enabled']['loginPageReady']
            or enabled.get('tabletEntry') is not ui['enabled']['tabletEntry']
            or ui['enabled']['tabletEntry'] is not True or enabled.get('moduleEnabled') is not True
            or smoke.get('qrPage') != ui['qrPage'] or smoke.get('qrStable') is not True):
        raise ValueError('WeChat runtime UI summary differs from captured hierarchy and activity files')

    manager_states = {}
    for stage in ('initial', 'disabled', 'enabled', 'final'):
        raw = _read_json(files[f'apps/wechat/manager-state-{stage}.json'], 'WeChat manager state')
        manager_states[stage] = runtime_policy.derive_manager_state(raw, 'io.github.nku100.impad', package)
    if (smoke.get('managerStates') != manager_states
            or smoke.get('initialManagerState') != manager_states['initial']
            or baseline.get('scopeVerified') is not True or enabled.get('scopeVerified') is not True
            or baseline.get('moduleEnabled') != manager_states['disabled']['moduleEnabled']
            or enabled.get('moduleEnabled') != manager_states['enabled']['moduleEnabled']
            or smoke.get('finalManagerState') != manager_states['final']
            or manager_states['disabled']['moduleEnabled'] is not False
            or manager_states['enabled']['moduleEnabled'] is not True
            or manager_states['final']['moduleEnabled'] is not True):
        raise ValueError('WeChat manager state summary differs from captured module/scope query rows')

    installed = _read_json(files['apps/wechat/installed-identities.json'], 'WeChat installed APK identity')
    runtime_policy.derive_installed_identities(
        installed, package, candidate.get('identity', {}).get('apkSha256'),
        'io.github.nku100.impad', module.get('sha256'))
    if smoke.get('installedIdentities') != installed:
        raise ValueError('WeChat installed APK identity summary differs from its captured raw file')
    if smoke.get('moduleApkSha256') != installed['module']['baseApkSha256']:
        raise ValueError('WeChat installed module APK digest differs from the tested module')

    processes, logcat = read_text('processes.txt'), read_text('logcat.txt')
    scoped = runtime_policy.scoped_module_log_lines(logcat, processes, package)
    if read_text('app-hook-logs.txt') != scoped:
        raise ValueError('WeChat scoped module logs differ from raw logcat and process ownership')
    diagnostics, lifecycle = runtime_policy.derive_hook_evidence(
        processes, scoped, candidate, module, installed['module'])
    if (smoke.get('runtimeDiagnostics') != diagnostics
            or smoke.get('runtimeLifecycleEvidence') != lifecycle):
        raise ValueError('WeChat hook, Tinker or native summary differs from captured runtime logs')
    if (diagnostics['compatibility'] != 'COMPATIBLE' or diagnostics['hookInstallation'] != 'INSTALLED_2'
            or lifecycle['hookCount'] != len(policy.required_hook_ids)
            or lifecycle['tinkerEvidence']['status'] != 'READY'
            or lifecycle['nativeEvidence']['status'] != 'LOADED'):
        raise ValueError('WeChat raw runtime lifecycle evidence does not satisfy the required hook gate')
    return files


def verify_runtime_rejection(smoke, evidence_root, candidate, module):
    """Prove a behavior mismatch from raw captures before allowing code adaptation."""
    if smoke.get('status') != 'RUNTIME_REJECTED':
        raise ValueError('Runtime rejection was not reported')
    app_id = candidate.get('appId')
    if app_id == 'qq':
        from scripts.ci.apps.qq import runtime_policy
        files = _validate_qq_raw_evidence(
            smoke, evidence_root, candidate, module, allow_app_id_mismatch=True)
        if runtime_policy.validate_request_evidence(
                smoke['requestIdentityEvidence'], candidate['identity']['apkSha256'],
                module['sha256'], candidate['requestIdentityExpectation']):
            raise ValueError('Raw QQ requests do not demonstrate a behavior mismatch')
        return files
    if app_id not in {'wechat', 'wecom'}:
        raise ValueError('Runtime rejection replay is unavailable for this application')
    if app_id == 'wechat':
        from scripts.ci.apps.wechat import runtime_policy
    else:
        from scripts.ci.apps.wecom import runtime_policy
    entries = smoke.get('evidence')
    if not isinstance(entries, list) or not entries:
        raise ValueError('Runtime rejection has no raw evidence references')
    files = {}
    for item in entries:
        if (not isinstance(item, dict) or not isinstance(item.get('path'), str)
                or not re.fullmatch(r'[a-f0-9]{64}', str(item.get('sha256', '')))):
            raise ValueError('Runtime rejection evidence reference is malformed')
        path = _resolve_child(evidence_root, item['path'], 'Runtime rejection evidence')
        if item['path'] in files or _sha256(path) != item['sha256']:
            raise ValueError('Runtime rejection evidence is duplicated or changed')
        files[item['path']] = path
    prefix = f'apps/{app_id}/'
    def read(name):
        if prefix + name not in files:
            raise ValueError(f'Runtime rejection evidence is missing: {name}')
        return files[prefix + name].read_text()

    policy = get_policy(app_id)
    package = policy.package_name
    if candidate.get('identity', {}).get('packageName') != package:
        raise ValueError('Runtime rejection candidate package differs from policy')
    installed = json.loads(read('installed-identities.json'))
    runtime_policy.derive_installed_identities(
        installed, package, candidate['identity']['apkSha256'],
        'io.github.nku100.impad', module['sha256'])
    for stage, enabled in (('initial', None), ('disabled', False), ('enabled', True), ('final', True)):
        state = runtime_policy.derive_manager_state(
            json.loads(read(f'manager-state-{stage}.json')), 'io.github.nku100.impad', package)
        if enabled is not None and state['moduleEnabled'] is not enabled:
            raise ValueError('Runtime rejection module toggle state is invalid')
    processes, logcat = read('processes.txt'), read('logcat.txt')
    scoped = runtime_policy.scoped_module_log_lines(logcat, processes, package)
    if read('app-hook-logs.txt') != scoped:
        raise ValueError('Runtime rejection scoped logs differ from raw process evidence')
    runtime_policy.derive_hook_evidence(processes, scoped, candidate, module, installed['module'])
    for label in ('module-off-login', 'module-on-login'):
        activity, hierarchy = read(f'{label}-activity.txt'), read(f'{label}.xml')
        # Parse ownership and visibility strictly; navigation or missing UI is not a rule failure.
        nodes = runtime_policy._app_nodes(hierarchy, package)
        component = runtime_policy._top_resumed_component(activity, package)
        if (not runtime_policy._activity_ends_with(component, runtime_policy.login_input_activity)
                or not runtime_policy.login_input_ready(activity, hierarchy, package)
                or not any(node.get('visible') != 'false' for node in nodes)):
            raise ValueError('Runtime rejection did not capture the application login screen')
        if any(node.get('visible') != 'false' and runtime_policy.tablet_entry_visible(node.get('text', ''))
               for node in nodes):
            raise ValueError('Raw UI does not demonstrate the missing tablet-entry failure')
    return files


def export_runtime_adaptation(report_path, candidate_path, evidence_root, operation):
    """Export a verified failure for the existing incompatible-candidate controller."""
    report_path, candidate_path = Path(report_path).resolve(), Path(candidate_path).resolve()
    report = _read_json(report_path, 'Runtime validation')
    if operation != 'adapt' or report.get('outcome') != 'RUNTIME_INCOMPATIBLE':
        return False
    candidate = _read_json(candidate_path, 'Candidate')
    if candidate != report.get('candidate') or candidate.get('status') != 'PREPARED':
        raise ValueError('Runtime failure does not match the prepared candidate')
    evidence_root = Path(evidence_root).resolve()
    references = [{'path': report_path.relative_to(candidate_path.parent).as_posix(),
                   'sha256': _sha256(report_path)}]
    for item in report['runtime']['evidence']:
        path = _resolve_child(evidence_root, item['path'], 'Runtime rejection evidence')
        if _sha256(path) != item['sha256']:
            raise ValueError('Runtime failure evidence changed before export')
        references.append({'path': path.relative_to(candidate_path.parent).as_posix(),
                           'sha256': item['sha256']})
    candidate.update(status='INCOMPATIBLE', runtimeFailure={
        'outcome': 'RUNTIME_INCOMPATIBLE', 'diagnostics': report['diagnostics'],
        'evidence': references})
    candidate_path.write_text(json.dumps(candidate, indent=2) + '\n')
    return True


def verify_validation_files(candidate_path, module_apk, regression_path, smoke_path,
                            evidence_root, conclusion, repository, acceptance_path,
                            initial_migration=False, acceptance_snapshot=None):
    """Reopen stage artifacts, recompute identities, and invoke the shared evaluator."""
    from scripts.ci.candidate_selection import prepare_module

    candidate_path = Path(candidate_path).resolve()
    candidate_root = candidate_path.parent
    candidate = _read_json(candidate_path, 'Candidate')
    if not isinstance(candidate, dict) or candidate.get('schemaVersion') != 1:
        raise ValueError('Candidate report schema is invalid')
    candidate['_root'] = str(candidate_root)
    static_path = _resolve_child(candidate_root, candidate.get('staticReport', ''), 'Static report')
    static_digest = _sha256(static_path)
    if static_digest != candidate.get('staticReportSha256'):
        raise ValueError('Static report changed after candidate preparation')
    static = _read_json(static_path, 'Static checker')

    apk_files = candidate.get('apkFiles')
    if not isinstance(apk_files, list) or not apk_files:
        raise ValueError('Candidate APK file list is missing')
    actual_inputs = []
    for index, item in enumerate(apk_files):
        role = 'base' if index == 0 else 'split'
        if (not isinstance(item, dict) or item.get('role') != role
                or not re.fullmatch(r'[a-f0-9]{64}', item.get('sha256', '').lower())):
            raise ValueError('Candidate base/split file list is malformed')
        path = _resolve_child(candidate_root, item.get('path', ''), 'Candidate APK')
        digest = _sha256(path)
        if digest != item['sha256'].lower():
            raise ValueError(f'Candidate APK changed after static analysis: {item["path"]}')
        actual_inputs.append(digest)
    if candidate['identity'].get('apkSha256', '').lower() != actual_inputs[0]:
        raise ValueError('Candidate base APK digest differs from its identity')
    static_input_rows = static.get('inputHashes')
    if not isinstance(static_input_rows, list) or len(static_input_rows) != len(actual_inputs):
        raise ValueError('Static report APK input list is incomplete')
    static_inputs = []
    for item in static_input_rows:
        if (not isinstance(item, dict) or not isinstance(item.get('path'), str)
                or Path(item['path']).is_absolute()
                or not re.fullmatch(r'[a-f0-9]{64}', item.get('sha256', '').lower())):
            raise ValueError('Static report APK input reference is malformed')
        static_input = _resolve_child(candidate_root, item['path'], 'Static checker APK input')
        digest = _sha256(static_input)
        if digest != item['sha256'].lower():
            raise ValueError('Static report APK input hash does not match its relative file')
        static_inputs.append(digest)
    if static_inputs != actual_inputs:
        raise ValueError('Static report input hashes do not bind the staged base/split APK files')

    module = prepare_module(Path(module_apk), static, Path(repository))
    module_apk = Path(module_apk).resolve()
    saved_module = candidate.get('module')
    stable_module_fields = ('sha256', 'sourceCommit', 'sourceTreeSha256', 'routeConfigurationSha256',
                            'dependencyLockSha256', 'ruleCatalogSha256', 'routeSha256',
                            'fingerprintFileSha256', 'fingerprint', 'route', 'dexkit')
    if (not isinstance(saved_module, dict)
            or any(saved_module.get(key) != module.get(key) for key in stable_module_fields)):
        raise ValueError('Candidate module identity differs from the APK build fingerprint')

    regression_path = Path(regression_path).resolve()
    regression_raw = _read_json(regression_path, 'Regression')
    if not isinstance(regression_raw, list) or not regression_raw:
        raise ValueError('Regression report must contain per-record results')
    from scripts.ci.app_policy import get_policy
    acceptance_snapshot_binding = None
    verified_acceptance_snapshot = None
    from scripts.ci.acceptance import AcceptanceSnapshot, read_acceptance_snapshot

    if acceptance_snapshot is None:
        verified_acceptance_snapshot = read_acceptance_snapshot(
            acceptance_path, candidate['appId'], initial_migration,
            catalog=static.get('ruleCatalog'), repository=repository)
    elif (not isinstance(acceptance_snapshot, AcceptanceSnapshot)
          or acceptance_snapshot.path != Path(acceptance_path).resolve()
          or acceptance_snapshot.initial_migration is not bool(initial_migration)):
        raise ValueError('Supplied acceptance snapshot does not match verifier inputs')
    else:
        verified_acceptance_snapshot = acceptance_snapshot
    regression_status, acceptance_snapshot_binding = _verify_regression_snapshot(
        regression_raw, verified_acceptance_snapshot, candidate['appId'], initial_migration,
        static.get('ruleCatalog'), repository)
    if candidate.get('appId') == 'qq' and regression_status != 'NO_HISTORICAL_SAMPLES':
        from scripts.ci.acceptance import validate_expected_plan
        for row in regression_raw:
            if row.get('status') != 'PASSED':
                continue
            reference = row.get('staticEvidence')
            if (not isinstance(reference, dict) or not isinstance(reference.get('path'), str)
                    or not re.fullmatch(r'[a-f0-9]{64}', reference.get('sha256', '').lower())):
                raise ValueError('Regression row has no raw static report reference')
            report_path = _resolve_child(regression_path.parent, reference['path'], 'Regression static report')
            if _sha256(report_path) != reference['sha256'].lower():
                raise ValueError('Regression static report hash changed')
            report = _read_json(report_path, 'Regression static checker')
            plan = row.get('expectedPlan')
            if (report.get('appId') != row.get('appId') or report.get('identity') != row.get('identity')
                    or report.get('analysisScope') != 'STATIC_ONLY'
                    or report.get('selection', {}).get('status') != 'SELECTED'
                    or report.get('selection', {}).get('selectedRuleId') != plan.get('ruleId')
                    or report.get('selection', {}).get('strategyId') != plan.get('strategyId')
                    or report.get('selection', {}).get('targets') != plan.get('targets')):
                raise ValueError('Regression raw static report differs from its fixed record')
            validate_expected_plan(plan, report.get('ruleCatalog', {}), row.get('identity', {}).get('versionCode'))
    regression = {'status': regression_status, 'records': regression_raw,
                  'evidence': {'path': regression_path.relative_to(candidate_root).as_posix()
                               if regression_path.is_relative_to(candidate_root) else str(regression_path),
                               'sha256': _sha256(regression_path)}}

    evidence_root = Path(evidence_root).resolve()
    smoke_path = _resolve_child(evidence_root, Path(smoke_path).resolve().relative_to(evidence_root).as_posix(),
                                'Runtime report')
    smoke = _read_json(smoke_path, 'Runtime')
    if not isinstance(smoke, dict):
        raise ValueError('Runtime report has an invalid schema')
    files = {}
    runtime_rejection = False
    if smoke.get('status') == 'RUNTIME_REJECTED':
        try:
            files = verify_runtime_rejection(smoke, evidence_root, candidate, module)
            runtime_rejection = True
        except (KeyError, TypeError, ValueError, OSError) as error:
            smoke['rejectionReplayError'] = str(error)
    elif smoke.get('status') == 'RUNTIME_SMOKE_VERIFIED' and candidate.get('appId') == 'qq':
        files = _validate_qq_raw_evidence(smoke, evidence_root, candidate, module)
    elif smoke.get('status') == 'RUNTIME_SMOKE_VERIFIED' and candidate.get('appId') == 'wecom':
        files = _validate_wecom_raw_evidence(smoke, evidence_root, candidate, module)
    elif smoke.get('status') == 'RUNTIME_SMOKE_VERIFIED' and candidate.get('appId') == 'wechat':
        files = _validate_wechat_raw_evidence(smoke, evidence_root, candidate, module)
    elif smoke.get('status') == 'RUNTIME_SMOKE_VERIFIED':
        entries = smoke.get('evidence')
        if not isinstance(entries, list) or not entries:
            raise ValueError('Runtime report has no raw evidence references')
        for item in entries:
            if (not isinstance(item, dict) or not isinstance(item.get('path'), str)
                    or not re.fullmatch(r'[a-f0-9]{64}', item.get('sha256', '').lower())):
                raise ValueError('Runtime evidence reference is malformed')
            path = _resolve_child(evidence_root, item['path'], 'Runtime evidence')
            if _sha256(path) != item['sha256'].lower():
                raise ValueError(f'Runtime evidence hash changed: {item["path"]}')
            files[item['path']] = path
    smoke['evidenceReport'] = {
        'path': smoke_path.relative_to(evidence_root).as_posix(), 'sha256': _sha256(smoke_path),
    }
    if smoke.get('status') == 'RUNTIME_SMOKE_VERIFIED':
        smoke['_verifiedEvidenceFiles'] = {path: _sha256(path) for path in files.values()}

    result = evaluate_validation(candidate, module, static, regression, smoke, conclusion,
                                 runtime_rejection=runtime_rejection)
    result['candidate'].pop('_root', None)
    result['runtime'].pop('_verifiedEvidenceFiles', None)
    try:
        runtime_ref_path = smoke_path.relative_to(candidate_root).as_posix()
    except ValueError:
        runtime_ref_path = str(smoke_path)
    result['stageEvidence'] = {
        'static': {'path': candidate['staticReport'], 'sha256': static_digest},
        'regression': regression['evidence'],
        'runtime': {'path': runtime_ref_path,
                    'sha256': smoke['evidenceReport']['sha256']},
    }
    if acceptance_snapshot_binding is not None:
        result['acceptanceSnapshot'] = acceptance_snapshot_binding
        result['_acceptanceSnapshotObject'] = verified_acceptance_snapshot
    result['inputPaths'] = {'candidate': str(candidate_path), 'moduleApk': str(module_apk),
                            'regression': str(regression_path), 'smoke': str(smoke_path),
                            'evidenceRoot': str(evidence_root), 'repository': str(Path(repository).resolve())}
    return result


def evaluate_validation(candidate, module, static, regression, smoke, conclusion, *, runtime_rejection=False):
    """Pure decision over parsed controller inputs; file verification belongs to the caller."""
    candidate = candidate if isinstance(candidate, dict) else {}
    module = module if isinstance(module, dict) else {}
    static = static if isinstance(static, dict) else {}
    regression = regression if isinstance(regression, dict) else {}
    smoke = smoke if isinstance(smoke, dict) else {}
    result = {'schemaVersion': 1, 'candidate': candidate, 'module': module, 'static': static,
              'regression': regression, 'runtime': smoke, 'outcome': 'ERROR',
              'failedStage': None, 'diagnostics': [], 'stageEvidence': {}}
    if conclusion in {'cancelled', 'skipped'}:
        result.update(outcome='CANCELLED', failedStage='runtime',
                      diagnostics=['Runtime acceptance did not complete'])
        return result
    if smoke.get('status') == 'ACCEPTANCE_UNAVAILABLE':
        result.update(outcome='UNAVAILABLE', failedStage='runtime',
                      diagnostics=[smoke.get('reason', 'Runtime acceptance environment was unavailable')])
        return result
    if (not isinstance(candidate, dict) or candidate.get('schemaVersion') != 1
            or candidate.get('status') != 'PREPARED'):
        result.update(outcome='CANDIDATE_REJECTED', failedStage='candidate',
                      diagnostics=['Candidate schema is invalid'])
        return result
    identity = candidate.get('identity', {})
    if not isinstance(identity, dict):
        result.update(outcome='CANDIDATE_REJECTED', failedStage='candidate',
                      diagnostics=['Candidate APK identity is invalid'])
        return result
    try:
        policy = policy_for_report({'appId': candidate['appId'], 'identity': identity}, candidate['appId'])
    except (KeyError, ValueError) as error:
        result.update(outcome='CANDIDATE_REJECTED', failedStage='candidate', diagnostics=[str(error)])
        return result
    apk_sha = identity.get('apkSha256', '').lower()
    apk_files = candidate.get('apkFiles', [])
    candidate_plan = candidate.get('expectedPlan')
    if not isinstance(module, dict):
        result.update(outcome='ERROR', failedStage='module', diagnostics=['Module identity is invalid'])
        return result
    try:
        validate_official_apk_url(candidate.get('sourceUrl', ''), policy.official_apk_prefix)
    except ValueError:
        apk_files = []
    if (identity.get('packageName') != policy.package_name
            or identity.get('abi') not in policy.supported_abis
            or identity.get('signerSha256', '').lower() != policy.signer_sha256
            or type(identity.get('versionCode')) is not int or identity['versionCode'] <= 0
            or not re.fullmatch(r'\d+(?:\.\d+)*', identity.get('versionName', ''))
            or not re.fullmatch(r'[a-f0-9]{64}', apk_sha) or not apk_files
            or not isinstance(apk_files, list) or not isinstance(apk_files[0], dict)
            or apk_files[0].get('role') != 'base' or apk_files[0].get('sha256', '').lower() != apk_sha
            or any(not isinstance(item, dict) or item.get('role') != ('base' if index == 0 else 'split')
                   or not isinstance(item.get('path'), str) or not item['path']
                   or not re.fullmatch(r'[a-f0-9]{64}', item.get('sha256', '').lower())
                   for index, item in enumerate(apk_files))
            or not isinstance(candidate_plan, dict) or candidate_plan.get('selection') != 'UNIQUE'):
        result.update(outcome='CANDIDATE_REJECTED', failedStage='candidate',
                      diagnostics=['Candidate APK identity or expected unique plan is invalid'])
        return result
    selection = static.get('selection')
    if (not isinstance(selection, dict) or static.get('analysisScope') != 'STATIC_ONLY'
            or static.get('appId') != policy.app_id or static.get('identity') != identity
            or selection.get('status') != 'SELECTED'):
        result.update(outcome='CANDIDATE_REJECTED', failedStage='static',
                      diagnostics=['Shared static check did not pass for this exact APK identity'])
        return result
    if (not isinstance(static.get('inputHashes'), list)
            or any(not isinstance(row, dict) for row in static['inputHashes'])):
        result.update(outcome='CANDIDATE_REJECTED', failedStage='static',
                      diagnostics=['Shared static checker returned malformed APK input hashes'])
        return result
    static_hashes = [row.get('sha256', '').lower() for row in static['inputHashes']]
    candidate_hashes = [row.get('sha256', '').lower() for row in apk_files]
    if candidate_hashes != static_hashes:
        result.update(outcome='CANDIDATE_REJECTED', failedStage='static',
                      diagnostics=['Static report input APK bytes do not match the staged candidate files'])
        return result
    expected = candidate_plan
    catalog = static.get('ruleCatalog', {})
    definitions = catalog.get('definitions', []) if isinstance(catalog, dict) else []
    definition = next((item for item in definitions if isinstance(item, dict)
                       and item.get('ruleId') == expected.get('ruleId')), None) if isinstance(definitions, list) else None
    targets = expected.get('targets')
    static_plan = {'selection': 'UNIQUE', 'ruleId': selection.get('selectedRuleId'),
                   'strategyId': selection.get('strategyId'), 'targets': selection.get('targets')}
    if (not isinstance(catalog, dict) or catalog.get('schemaVersion') != 1
            or catalog.get('appId') != policy.app_id
            or catalog.get('ruleCatalogSha256') != static.get('ruleCatalogSha256')
            or not definition or definition.get('strategyId') != expected.get('strategyId')
            or not isinstance(targets, dict)
            or set(targets) != set(definition.get('requiredTargetIds', []))
            or static_plan != expected):
        result.update(outcome='CANDIDATE_REJECTED', failedStage='static',
                      diagnostics=['Static plan does not match the current catalog or complete candidate plan'])
        return result
    try:
        from scripts.ci.acceptance import validate_expected_plan
        validate_expected_plan(expected, catalog, identity.get('versionCode'))
    except (TypeError, ValueError) as error:
        result.update(outcome='CANDIDATE_REJECTED', failedStage='static', diagnostics=[str(error)])
        return result
    route_candidates = static.get('routeCandidates')
    route = candidate.get('route')
    route_projection = {'appId': policy.app_id, 'candidates': route_candidates}
    if (not isinstance(route_candidates, list) or not route_candidates
            or any(not isinstance(rule_id, str) or not rule_id for rule_id in route_candidates)
            or len(set(route_candidates)) != len(route_candidates)
            or expected['ruleId'] not in route_candidates
            or not isinstance(route, dict)
            or set(route) != {'appId', 'candidates', 'samplingMode'}
            or route.get('appId') != policy.app_id
            or route.get('candidates') != route_candidates
            or route.get('samplingMode') != ('DIRECT' if len(route_candidates) == 1 else 'GAP')
            or any(rule_id not in {row.get('ruleId') for row in definitions if isinstance(row, dict)}
                   for rule_id in route_candidates)):
        result.update(outcome='CANDIDATE_REJECTED', failedStage='static',
                      diagnostics=['Candidate route does not match the shared static route candidates'])
        return result
    route_sha = hashlib.sha256(json.dumps(
        route_projection, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    if (candidate.get('routeSha256') != route_sha
            or module.get('routeSha256') != route_sha
            or module.get('route') != route_projection):
        result.update(outcome='CANDIDATE_REJECTED', failedStage='static',
                      diagnostics=['Candidate and module route fingerprints do not match the shared static route'])
        return result
    execution = static.get('executionIdentity', {})
    if (static.get('ruleCatalogSha256') != module.get('ruleCatalogSha256')
            or candidate.get('ruleCatalogSha256') != module.get('ruleCatalogSha256')
            or candidate.get('routeSha256') != module.get('routeSha256')
            or execution.get('fingerprintFileSha256') != module.get('fingerprintFileSha256')
            or execution.get('sourceTreeSha256') != module.get('sourceTreeSha256')
            or execution.get('routeConfigurationSha256') != module.get('routeConfigurationSha256')
            or execution.get('dependencyLockSha256') != module.get('dependencyLockSha256')):
        result.update(outcome='CANDIDATE_REJECTED', failedStage='static',
                      diagnostics=['Static plan or source APK bytes do not match the candidate'])
        return result
    required_module = ('sha256', 'sourceTreeSha256', 'ruleCatalogSha256', 'routeSha256', 'dependencyLockSha256')
    required_module += ('routeConfigurationSha256', 'fingerprintFileSha256')
    if any(not re.fullmatch(r'[a-f0-9]{64}', module.get(key, '').lower()) for key in required_module):
        result.update(outcome='ERROR', failedStage='module', diagnostics=['Module source identity is incomplete'])
        return result
    if regression.get('status') == 'FAILED':
        result.update(outcome='REGRESSION_FAILED', failedStage='regression',
                      diagnostics=['A fixed historical acceptance representative failed'])
        return result
    if regression.get('status') not in {'PASSED', 'NO_HISTORICAL_SAMPLES'}:
        result.update(outcome='ERROR', failedStage='regression',
                      diagnostics=['Regression status is missing or invalid'])
        return result
    confirmed_rejection = (runtime_rejection and conclusion in {'success', 'failure'}
                           and smoke.get('status') == 'RUNTIME_REJECTED')
    if not confirmed_rejection and (conclusion != 'success' or smoke.get('status') != 'RUNTIME_SMOKE_VERIFIED'):
        result.update(outcome='ERROR', failedStage='runtime',
                      diagnostics=['Runtime behavior evidence is absent or rejected'])
        return result
    exact = (smoke.get('appId') == policy.app_id and smoke.get('packageName') == policy.package_name
             and smoke.get('abi') == identity.get('abi') and smoke.get('apkSha256') == apk_sha
             and smoke.get('moduleApkSha256') == module.get('sha256')
             and smoke.get('moduleSourceTreeSha256') == module.get('sourceTreeSha256')
             and smoke.get('ruleCatalogSha256') == module.get('ruleCatalogSha256')
             and smoke.get('routeSha256') == module.get('routeSha256')
             and smoke.get('dependencyLockSha256') == module.get('dependencyLockSha256'))
    if not exact:
        result.update(outcome='ERROR', failedStage='runtime',
                      diagnostics=['Runtime evidence does not match the tested APK/module/source'])
        return result
    if confirmed_rejection:
        result.update(outcome='RUNTIME_INCOMPATIBLE', failedStage='runtime',
                      diagnostics=[smoke.get('reason', 'Raw runtime captures prove a behavior mismatch')])
        return result
    if policy.app_id == 'qq':
        from scripts.ci.apps.qq.runtime_policy import validate_request_evidence
        request_ok = (smoke.get('acceptanceKind') == 'REQUEST_APP_ID_CONTRAST'
                      and smoke.get('requestIdentityExpectation') == candidate.get('requestIdentityExpectation')
                      and validate_request_evidence(smoke.get('requestIdentityEvidence'), apk_sha,
                                                    module['sha256'], smoke.get('requestIdentityExpectation')))
        lifecycle_ok = (smoke.get('nativeEvidence', {}).get('status') == 'LOADED'
                        and smoke.get('nativeEvidence', {}).get('apiSha256') == module.get('dexkit', {}).get('dexkitAndroidApiSha256')
                        and smoke.get('nativeEvidence', {}).get('nativeSha256') == module.get('dexkit', {}).get('dexkitAndroidArm64NativeSha256')
                        and smoke.get('qfixEvidence', {}).get('status') == 'READY'
                        and smoke.get('qfixEvidence', {}).get('contextPackage') == policy.package_name
                        and smoke.get('timingEvidence', {}).get('qfixReadyBeforeNative') is True
                        and smoke.get('timingEvidence', {}).get('hookInstalledBeforeRequest') is True)
        if not request_ok or not lifecycle_ok:
            result.update(outcome='ERROR', failedStage='runtime',
                          diagnostics=['QQ request contrast, native/QFix, or hook timing evidence is incomplete'])
            return result
    evidence = smoke.get('evidence')
    if (not isinstance(evidence, list) or not evidence
            or any(not isinstance(item, dict) or not item.get('path')
                   or not re.fullmatch(r'[a-f0-9]{64}', item.get('sha256', '').lower())
                   for item in evidence)):
        result.update(outcome='ERROR', failedStage='runtime',
                      diagnostics=['Raw runtime evidence references and hashes are required'])
        return result
    stage_evidence = {
        'static': {'path': candidate.get('staticReport'), 'sha256': candidate.get('staticReportSha256')},
        'regression': regression.get('evidence'),
        'runtime': smoke.get('evidenceReport'),
    }
    if any(not isinstance(ref, dict) or not ref.get('path')
           or not re.fullmatch(r'[a-f0-9]{64}', str(ref.get('sha256', '')).lower())
           for ref in stage_evidence.values()):
        result.update(outcome='ERROR', failedStage='evidence',
                      diagnostics=['Static, regression, and runtime report references are required'])
        return result
    result.update(outcome='VERIFIED', failedStage=None, stageEvidence=stage_evidence,
                  regression=dict(regression, status=regression['status']))
    return result


def runtime_result(report, smoke, conclusion, app_id=None):
    if report['status'] != 'STATIC_VERIFIED_PENDING_RUNTIME':
        raise ValueError('Runtime acceptance requires static verification')
    policy = policy_for_report(report, app_id)
    identity = report['identity']
    if identity.get('abi') not in policy.supported_abis:
        raise ValueError('Candidate ABI does not match the registered app policy')
    if identity.get('signerSha256', '').lower() != policy.signer_sha256:
        raise ValueError('Candidate signer does not match the registered app policy')
    result = copy.deepcopy(report)
    result['appId'] = policy.app_id
    if conclusion in {'cancelled', 'skipped'}:
        result['runtimeConclusion'] = conclusion
        result['blockers'] = ['Hosted runtime smoke did not complete; rerun runtime verification for this exact build.']
        return result
    verified = (
        conclusion == 'success'
        and smoke.get('status') == 'RUNTIME_SMOKE_VERIFIED'
        and smoke.get('appId') == policy.app_id
        and smoke.get('packageName') == policy.package_name
        and smoke.get('abi') == identity.get('abi')
        and smoke.get('baseline') == policy.runtime_baseline
        and smoke.get('hookIds') == list(policy.required_hook_ids)
        and (policy.app_id == 'qq' or smoke.get('qrPage') == policy.runtime_qr_page)
        and smoke.get('apkSha256') == identity.get('apkSha256')
    )
    if policy.app_id == 'qq':
        from scripts.ci.apps.qq.runtime_policy import validate_request_evidence
        if smoke.get('status') == 'ACCEPTANCE_UNAVAILABLE':
            result.update(status='STATIC_VERIFIED_PENDING_RUNTIME', runtimeConclusion='acceptance-unavailable',
                          blockers=['QQ request identity observation is unavailable; no runtime promotion.'])
            return result
        verified = verified and smoke.get('acceptanceKind') == 'REQUEST_APP_ID_CONTRAST' and validate_request_evidence(
            smoke.get('requestIdentityEvidence'), identity.get('apkSha256'), smoke.get('moduleApkSha256'), report.get('requestIdentityExpectation'))
    result['status'] = 'RUNTIME_VERIFIED' if verified else 'RUNTIME_REJECTED'
    result['blockers'] = [] if verified else ['Hosted runtime smoke failed or exact-build evidence is missing.']
    return result


def verify_cli(argv):
    parser = argparse.ArgumentParser(description='Revalidate Task5 evidence from raw stage artifacts')
    parser.add_argument('--app-id', required=True)
    parser.add_argument('--candidate', type=Path, required=True)
    parser.add_argument('--module-apk', type=Path, required=True)
    parser.add_argument('--regression', type=Path, required=True)
    parser.add_argument('--smoke', type=Path, required=True)
    parser.add_argument('--evidence-root', type=Path, required=True)
    parser.add_argument('--acceptance', type=Path, required=True)
    parser.add_argument('--repository', type=Path, default=Path.cwd())
    parser.add_argument('--conclusion', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    report = verify_validation_files(args.candidate, args.module_apk, args.regression,
                                     args.smoke, args.evidence_root, args.conclusion,
                                     args.repository, args.acceptance, False)
    if report['candidate'].get('appId') != args.app_id:
        raise ValueError('Candidate appId does not match the explicit verifier appId')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    output_report = dict(report)
    output_report.pop('_acceptanceSnapshotObject', None)
    args.output.write_text(json.dumps(output_report, indent=2) + '\n')
    print(json.dumps({'schemaVersion': report['schemaVersion'], 'outcome': report['outcome'],
                      'failedStage': report['failedStage']}))


def register_cli(argv):
    parser = argparse.ArgumentParser(description='Trusted, file-backed acceptance registration')
    parser.add_argument('--app-id', required=True)
    parser.add_argument('--candidate', type=Path, required=True)
    parser.add_argument('--module-apk', type=Path, required=True)
    parser.add_argument('--regression', type=Path, required=True)
    parser.add_argument('--smoke', type=Path, required=True)
    parser.add_argument('--evidence-root', type=Path, required=True)
    parser.add_argument('--acceptance', type=Path, required=True)
    parser.add_argument('--repository', type=Path, default=Path.cwd())
    parser.add_argument('--conclusion', required=True)
    parser.add_argument('--expected-head', required=True)
    parser.add_argument('--cancelled', action='store_true')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    if args.cancelled or args.conclusion != 'success':
        raise ValueError('Cancelled or unsuccessful validation cannot register an acceptance record')
    actor = os.environ.get('GITHUB_ACTOR', '')
    owner = os.environ.get('GITHUB_REPOSITORY_OWNER', '')
    if not actor or not owner or actor.casefold() != owner.casefold():
        raise ValueError('Acceptance registration requires the trusted repository owner as workflow actor')
    repository = Path(args.repository).resolve()
    expected_head = args.expected_head
    if not re.fullmatch(r'[a-fA-F0-9]{40,64}', expected_head):
        raise ValueError('Trusted source head is malformed')
    current_head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=repository,
                                           text=True).strip()
    if current_head != expected_head:
        raise ValueError('Trusted source head changed before acceptance registration')
    try:
        acceptance_relative = Path(args.acceptance).resolve().relative_to(repository).as_posix()
    except ValueError as error:
        raise ValueError('Acceptance path must be inside the trusted source checkout') from error
    if acceptance_relative != f'compatibility/{args.app_id}/acceptance.json':
        raise ValueError('Acceptance path does not match the explicit app policy')
    try:
        policy = get_policy(args.app_id)
    except ValueError as error:
        parser.error(str(error))
    report = verify_validation_files(args.candidate, args.module_apk, args.regression,
                                     args.smoke, args.evidence_root, args.conclusion,
                                     repository, args.acceptance, False)
    if (report.get('outcome') != 'VERIFIED'
            or report.get('candidate', {}).get('appId') != args.app_id):
        raise ValueError('Current file-backed validation is not VERIFIED for the requested app')
    from scripts.ci.acceptance import make_record
    record = make_record(
        report['candidate'], report['module'], report,
        candidate_path=args.candidate, module_apk=args.module_apk,
        regression_path=args.regression, smoke_path=args.smoke,
        evidence_root=args.evidence_root, acceptance_path=args.acceptance,
        initial_migration=False, repository=repository,
        conclusion=args.conclusion,
    )
    current_after_validation = subprocess.check_output(
        ['git', 'rev-parse', 'HEAD'], cwd=repository, text=True).strip()
    if current_after_validation != expected_head:
        raise ValueError('Trusted source head changed during acceptance registration')
    from scripts.ci.acceptance import validate_acceptance_snapshot
    snapshot = report.get('_acceptanceSnapshotObject')
    records = validate_acceptance_snapshot(snapshot, args.app_id, False,
                                           catalog=report['static'].get('ruleCatalog'),
                                           repository=repository)
    table = {'schemaVersion': 1, 'appId': args.app_id, 'records': records + [record]}
    acceptance_path = Path(args.acceptance)
    acceptance_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = acceptance_path.with_name(acceptance_path.name + '.tmp')
    temporary.write_text(json.dumps(table, indent=2) + '\n')
    os.replace(temporary, acceptance_path)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({'schemaVersion': 1, 'status': 'REGISTERED',
                                       'recordId': record['recordId'],
                                       'acceptancePath': acceptance_relative}, indent=2) + '\n')
    print(json.dumps({'schemaVersion': 1, 'status': 'REGISTERED', 'recordId': record['recordId']}))


def main():
    import sys
    if sys.argv[1:2] == ['verify']:
        verify_cli(sys.argv[2:])
        return
    if sys.argv[1:2] == ['register']:
        register_cli(sys.argv[2:])
        return
    parser = argparse.ArgumentParser()
    parser.add_argument('--app-id', required=True)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--smoke', type=Path, required=True)
    parser.add_argument('--conclusion', required=True)
    args = parser.parse_args()
    directory = args.directory
    report = json.loads((directory / 'candidate-report.json').read_text())
    smoke = json.loads(args.smoke.read_text()) if args.smoke.is_file() else {}
    result = runtime_result(report, smoke, args.conclusion, args.app_id)
    output = directory / 'runtime-result.json'
    output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps({'status': result['status'], 'report': str(output)}))


if __name__ == '__main__':
    main()
