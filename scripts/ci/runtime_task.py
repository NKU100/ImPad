"""Verify acceptance records and register only fully validated runtime evidence."""
import argparse
import copy
import hashlib
import json
import os
import subprocess
from pathlib import Path
import re
import uuid

from scripts.ci.app_policy import get_policy, policy_for_report
from scripts.ci.download_url import validate_download_url


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _regression_baseline_binding(baseline):
    bundle = baseline.migration_bundle
    return {
        'sha256': baseline.sha256,
        'appId': baseline.app_id,
        'migrationHistorySha256': bundle.history_sha256,
        'migrationExpectationsSha256': bundle.expectations_sha256,
        'v2Path': f'compatibility/{baseline.app_id}/acceptance-v2.json',
        'v2SnapshotSha256': baseline.v2_sha256,
        'regressionRecordIds': [case['recordId'] for case in baseline.cases],
        'sourceCases': [{'kind': case['kind'], 'recordId': case['recordId'],
                         'profileId': case['profileId'],
                         'sourceSha256': case['acceptanceSnapshotSha256']}
                        for case in baseline.cases],
    }


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


def _semantic_stage_context(candidate, module, static, regression, repository,
                            acceptance_path=None, migration_bundle=None,
                            regression_baseline=None):
    from scripts.ci.semantic_migration import load_migration_bundle
    from scripts.ci.semantic_regression_baseline import load_semantic_regression_baseline
    from scripts.ci.semantic_validation import validate_semantic_stage_models

    repository = Path(repository).resolve()
    bundle = migration_bundle or load_migration_bundle(repository)
    baseline = regression_baseline or load_semantic_regression_baseline(
        repository, candidate['appId'], bundle)
    baseline.assert_unchanged()
    expected_acceptance = repository / 'compatibility' / candidate['appId'] / 'acceptance.json'
    supplied_acceptance = Path(acceptance_path or expected_acceptance).resolve()
    if supplied_acceptance != expected_acceptance.resolve():
        raise ValueError('Acceptance path must be the pinned v1 history for this application')
    profiles = [row for row in bundle.profiles if row['appId'] == candidate['appId']]
    pinned_hashes = {row['historicalAcceptanceSha256'] for row in profiles}
    if len(pinned_hashes) != 1 or _sha256(supplied_acceptance) not in pinned_hashes:
        raise ValueError('Acceptance history bytes differ from the pinned migration bundle')

    records = regression.get('records')
    if not isinstance(records, list):
        raise ValueError('Regression report must contain fixed profile rows')
    evidence_ref = regression.get('evidence')
    if not isinstance(evidence_ref, dict) or not isinstance(evidence_ref.get('path'), str):
        raise ValueError('Regression report file reference is missing')
    regression_file = Path(evidence_ref['path']).resolve()
    if not regression_file.is_file() or _sha256(regression_file) != evidence_ref.get('sha256', '').lower():
        raise ValueError('Regression report changed after generation')
    regression_root = regression_file.parent
    fixed_reports = {}
    for row in records:
        reference = row.get('staticEvidence') if isinstance(row, dict) else None
        if (not isinstance(reference, dict) or not isinstance(reference.get('path'), str)
                or not re.fullmatch(r'[a-fA-F0-9]{64}', str(reference.get('sha256', '')))):
            raise ValueError('Fixed regression row has no hashed static report reference')
        report_path = _resolve_child(regression_root, reference['path'], 'Fixed regression static report')
        if _sha256(report_path) != reference['sha256'].lower():
            raise ValueError('Fixed regression static report changed')
        report = _read_json(report_path, 'Fixed regression static report')
        if not isinstance(report, dict):
            raise ValueError('Fixed regression static report has an invalid schema')
        fixed_reports[row.get('recordId')] = report

    gate = validate_semantic_stage_models(candidate, module, static, records, bundle,
                                          fixed_reports, repository=repository,
                                          baseline=baseline)
    baseline.assert_unchanged()
    return bundle, gate, baseline


def _validate_qq_raw_evidence(smoke, evidence_root, candidate, module, *, allow_app_id_mismatch=False):
    from scripts.ci.apps.qq import packet_capture, request_protocol, runtime_policy
    from scripts.ci.redroid_app_smoke import extract_qq_semantic_lifecycle_evidence
    from scripts.ci.apk_inputs import verify_installed_apk_hashes

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

    if len(candidate.get('apkFiles', [])) > 1:
        for label in ('disabled', 'enabled'):
            for stage in ('before', 'after'):
                key = f'apps/qq/installed-apks-{label}-{stage}.json'
                if key not in files:
                    raise ValueError('QQ split APK installed identity evidence is missing')
                installed = _read_json(files[key], 'QQ installed APK identities')
                if installed.get('packageName') != candidate['identity']['packageName']:
                    raise ValueError('QQ installed APK package differs from the candidate')
                verify_installed_apk_hashes(installed, candidate['apkFiles'])
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

    log_path = files.get('apps/qq/logcat-enabled.txt')
    process_path = files.get('apps/qq/processes-enabled.txt')
    if log_path is None or process_path is None:
        raise ValueError('QQ enabled logcat and process-table evidence are required')
    owner_pids = {item['pid'] for item in captures['enabled'].get('owners', [])}
    request_times = [frame.get('firstObservedMonotonicNs')
                     for frame in captures['enabled'].get('frames', [])]
    diagnostics, lifecycle = extract_qq_semantic_lifecycle_evidence(
        log_path.read_text(), owner_pids, request_times, candidate, module,
        process_path.read_text(), installed_module=None)
    if (smoke.get('runtimeDiagnostics') != diagnostics
            or smoke.get('runtimeLifecycleEvidence') != lifecycle):
        raise ValueError('QQ semantic lifecycle summary differs from hashed process and log evidence')
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
        'io.github.nku100.impad', module.get('sha256'), apk_files=candidate.get('apkFiles'))
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
        'io.github.nku100.impad', module.get('sha256'), apk_files=candidate.get('apkFiles'))
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
        'io.github.nku100.impad', module['sha256'], apk_files=candidate.get('apkFiles'))
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
                            initial_migration=False, acceptance_snapshot=None,
                            regression_baseline=None):
    """Reopen stage artifacts, recompute identities, and invoke the shared evaluator."""
    from scripts.ci.candidate_selection import prepare_module
    from scripts.ci.semantic_migration import load_migration_bundle
    from scripts.ci.semantic_regression_baseline import load_semantic_regression_baseline

    repository = Path(repository).resolve()
    if initial_migration:
        raise ValueError('Semantic verification requires the pinned historical migration bundle')
    migration_bundle = load_migration_bundle(repository)
    regression_baseline = regression_baseline or load_semantic_regression_baseline(
        repository, _read_json(candidate_path, 'Candidate').get('appId'), migration_bundle)
    regression_baseline.assert_unchanged()
    candidate_path = Path(candidate_path).resolve()
    candidate_root = candidate_path.parent
    candidate = _read_json(candidate_path, 'Candidate')
    if not isinstance(candidate, dict) or candidate.get('schemaVersion') != 2:
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

    module = prepare_module(Path(module_apk), static, repository)
    module_apk = Path(module_apk).resolve()
    saved_module = candidate.get('module')
    stable_module_fields = ('schemaVersion', 'sha256', 'sourceCommit', 'sourceTreeSha256',
                            'routeConfigurationSha256', 'dependencyLockSha256',
                            'ruleCatalogSha256', 'recognition', 'recognitionSha256',
                            'fingerprintFileSha256', 'fingerprint', 'dexkit')
    if (not isinstance(saved_module, dict)
            or any(saved_module.get(key) != module.get(key) for key in stable_module_fields)):
        raise ValueError('Candidate module identity differs from the APK build fingerprint')

    regression_path = Path(regression_path).resolve()
    regression_raw = _read_json(regression_path, 'Regression')
    if not isinstance(regression_raw, list) or not regression_raw:
        raise ValueError('Regression report must contain per-record results')
    regression_status = 'PASSED' if all(isinstance(row, dict) and row.get('status') == 'PASSED'
                                         for row in regression_raw) else 'FAILED'
    regression = {'status': regression_status, 'records': regression_raw,
                  'evidence': {'path': str(regression_path),
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
                                 runtime_rejection=runtime_rejection, repository=repository,
                                 acceptance_path=acceptance_path, migration_bundle=migration_bundle,
                                 regression_baseline=regression_baseline)
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
    reloaded_bundle = load_migration_bundle(repository)
    if (reloaded_bundle.history_sha256 != migration_bundle.history_sha256
            or reloaded_bundle.expectations_sha256 != migration_bundle.expectations_sha256):
        raise ValueError('Semantic migration bundle changed during runtime verification')
    regression_baseline.assert_unchanged()
    result['_migrationBundleObject'] = migration_bundle
    result['_regressionBaselineObject'] = regression_baseline
    result['inputPaths'] = {'candidate': str(candidate_path), 'moduleApk': str(module_apk),
                            'regression': str(regression_path), 'smoke': str(smoke_path),
                            'evidenceRoot': str(evidence_root), 'repository': str(Path(repository).resolve())}
    return result


def evaluate_validation(candidate, module, static, regression, smoke, conclusion, *, runtime_rejection=False,
                        repository=None, acceptance_path=None, migration_bundle=None,
                        regression_baseline=None):
    """Validate semantic stage models and runtime acceptance evidence."""
    candidate = candidate if isinstance(candidate, dict) else {}
    module = module if isinstance(module, dict) else {}
    static = static if isinstance(static, dict) else {}
    regression = regression if isinstance(regression, dict) else {}
    smoke = smoke if isinstance(smoke, dict) else {}
    result = {'schemaVersion': 2, 'candidate': candidate, 'module': module, 'static': static,
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
    if (not isinstance(candidate, dict) or candidate.get('schemaVersion') != 2
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
        validate_download_url(candidate.get('sourceUrl', ''))
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
            or not isinstance(candidate_plan, dict)
            or not isinstance(candidate.get('expectedExecutionSha256'), str)):
        result.update(outcome='CANDIDATE_REJECTED', failedStage='candidate',
                      diagnostics=['Candidate APK identity or expected complete plan is invalid'])
        return result
    if regression.get('status') == 'FAILED' or any(
            isinstance(row, dict) and row.get('status') == 'FAILED'
            for row in regression.get('records', [])):
        result.update(outcome='REGRESSION_FAILED', failedStage='regression',
                      diagnostics=['A fixed semantic regression representative failed'])
        return result
    repository = Path(repository or Path.cwd()).resolve()
    try:
        bundle, gate, regression_baseline = _semantic_stage_context(
            candidate, module, static, regression, repository, acceptance_path,
            migration_bundle, regression_baseline)
    except (OSError, TypeError, ValueError, KeyError) as error:
        result.update(outcome='CANDIDATE_REJECTED', failedStage='static', diagnostics=[str(error)])
        return result
    result['migrationBundle'] = {
        'historySha256': gate['migrationHistorySha256'],
        'expectationsSha256': gate['migrationExpectationsSha256'],
        'recordIds': gate['recordIds'],
    }
    result['regressionBaseline'] = gate['regressionBaseline']
    result['regressionRecordIds'] = gate['regressionRecordIds']
    result['baselineSha256'] = gate['baselineSha256']
    result['v2SnapshotSha256'] = gate['v2SnapshotSha256']
    result['_regressionBaselineObject'] = regression_baseline
    required_module = ('sha256', 'sourceTreeSha256', 'ruleCatalogSha256', 'recognitionSha256',
                       'dependencyLockSha256', 'routeConfigurationSha256', 'fingerprintFileSha256')
    if any(not re.fullmatch(r'[a-f0-9]{64}', str(module.get(key, '')).lower()) for key in required_module):
        result.update(outcome='ERROR', failedStage='module', diagnostics=['Module source identity is incomplete'])
        return result
    if regression.get('status') != 'PASSED':
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
             and smoke.get('recognitionSha256') == module.get('recognitionSha256')
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
        lifecycle = smoke.get('runtimeLifecycleEvidence', {})
        native = lifecycle.get('nativeEvidence', {})
        qfix = lifecycle.get('qfixEvidence', {})
        semantic_plan = lifecycle.get('semanticPlanEvidence', {})
        hook = lifecycle.get('hookEvidence', {})
        timing = lifecycle.get('timingEvidence', {})
        lifecycle_ok = (native.get('status') == 'LOADED'
                        and native.get('apiSha256') == module.get('dexkit', {}).get('dexkitAndroidApiSha256')
                        and native.get('nativeSha256') == module.get('dexkit', {}).get('dexkitAndroidArm64NativeSha256')
                        and qfix.get('status') == 'READY'
                        and qfix.get('contextPackage') == policy.package_name
                        and semantic_plan.get('status') == 'RESOLVED'
                        and semantic_plan.get('protocolVersion') == 2
                        and semantic_plan.get('executionSha256') == candidate.get('expectedExecutionSha256')
                        and hook.get('status') == 'INSTALLED'
                        and hook.get('protocolVersion') == 2
                        and hook.get('executionSha256') == candidate.get('expectedExecutionSha256')
                        and hook.get('completePlan') == candidate.get('expectedPlan')
                        and hook.get('processName') == f'{policy.package_name}:MSF'
                        and hook.get('hookCount') == len(candidate.get('expectedPlan', {}).get('hooks', []))
                        and timing.get('qfixBeforeNative') is True
                        and timing.get('nativeBeforePlan') is True
                        and timing.get('planBeforeHook') is True
                        and timing.get('compatibleBeforeHook') is True)
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
    from scripts.ci.semantic_migration import load_migration_bundle
    latest_bundle = load_migration_bundle(repository)
    if (latest_bundle.history_sha256 != bundle.history_sha256
            or latest_bundle.expectations_sha256 != bundle.expectations_sha256):
        result.update(outcome='ERROR', failedStage='static',
                      diagnostics=['Semantic migration bundle changed during verification'])
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
    output_report.pop('_migrationBundleObject', None)
    output_report.pop('_regressionBaselineObject', None)
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
    from scripts.ci.acceptance import (
        SEMANTIC_ACCEPTANCE_FILENAME, read_semantic_acceptance_table,
    )
    from scripts.ci.semantic_regression_baseline import load_semantic_regression_baseline
    regression_baseline = load_semantic_regression_baseline(repository, args.app_id)
    regression_baseline.assert_unchanged()
    semantic_acceptance_path = repository / 'compatibility' / args.app_id / SEMANTIC_ACCEPTANCE_FILENAME
    if not semantic_acceptance_path.parent.resolve().is_relative_to(repository):
        raise ValueError('Semantic acceptance output directory escapes the trusted repository')
    semantic_acceptance_bytes = read_semantic_acceptance_table(
        semantic_acceptance_path, args.app_id, repository=repository)
    try:
        policy = get_policy(args.app_id)
    except ValueError as error:
        parser.error(str(error))
    report = verify_validation_files(args.candidate, args.module_apk, args.regression,
                                     args.smoke, args.evidence_root, args.conclusion,
                                     repository, args.acceptance, False,
                                     regression_baseline=regression_baseline)
    if (report.get('outcome') != 'VERIFIED'
            or report.get('candidate', {}).get('appId') != args.app_id):
        raise ValueError('Current file-backed validation is not VERIFIED for the requested app')
    if report.get('regressionBaseline') != _regression_baseline_binding(regression_baseline):
        raise ValueError('Current validation does not bind the complete regression baseline')
    revalidated = verify_validation_files(
        args.candidate, args.module_apk, args.regression, args.smoke,
        args.evidence_root, args.conclusion, repository, args.acceptance, False,
        acceptance_snapshot=report.get('_acceptanceSnapshotObject'),
        regression_baseline=regression_baseline,
    )
    compared_fields = ('outcome', 'candidate', 'module', 'static', 'regression', 'runtime',
                       'stageEvidence', 'acceptanceSnapshot', 'migrationBundle', 'inputPaths',
                       'regressionBaseline', 'regressionRecordIds', 'baselineSha256',
                       'v2SnapshotSha256')
    if any(report.get(field) != revalidated.get(field) for field in compared_fields):
        raise ValueError('File-backed validation changed during acceptance registration')
    from scripts.ci.acceptance import (
        archive_verified_evidence, assert_evidence_archive_unchanged,
        build_semantic_acceptance_record,
        cleanup_new_evidence_archive, write_semantic_acceptance_table,
    )
    record_id = str(uuid.uuid4())
    evidence_archive = None
    evidence_paths = None
    try:
        evidence_archive, evidence_paths = archive_verified_evidence(
            revalidated, repository, record_id)
        assert_evidence_archive_unchanged(repository, evidence_archive)
        record = build_semantic_acceptance_record(
            revalidated, evidence_archive=evidence_archive, record_id=record_id)

        def assert_trusted_head():
            current = subprocess.check_output(
                ['git', 'rev-parse', 'HEAD'], cwd=repository, text=True).strip()
            if current != expected_head:
                raise ValueError('Trusted source head changed during acceptance registration')
            regression_baseline.assert_unchanged()
            assert_evidence_archive_unchanged(repository, evidence_archive)

        assert_trusted_head()
        table = write_semantic_acceptance_table(
            semantic_acceptance_path, record, args.app_id,
            expected_existing_bytes=semantic_acceptance_bytes,
            repository=repository,
            before_replace=assert_trusted_head,
        )
    except BaseException:
        if evidence_archive is not None:
            try:
                cleanup_new_evidence_archive(repository, evidence_archive)
            except Exception:
                pass
        raise
    semantic_acceptance_relative = semantic_acceptance_path.relative_to(repository).as_posix()
    evidence_manifest = {'path': evidence_archive['manifestPath'],
                         'sha256': evidence_archive['manifestSha256']}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({'schemaVersion': 2, 'status': 'REGISTERED',
                                       'recordId': record['recordId'],
                                       'acceptancePath': semantic_acceptance_relative,
                                       'evidencePaths': evidence_paths,
                                       'evidenceManifest': evidence_manifest,
                                       'recordCount': len(table['records'])}, indent=2) + '\n')
    print(json.dumps({'schemaVersion': 2, 'status': 'REGISTERED',
                      'recordId': record['recordId'],
                      'acceptancePath': semantic_acceptance_relative,
                      'evidencePaths': evidence_paths,
                      'evidenceManifest': evidence_manifest}))


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
