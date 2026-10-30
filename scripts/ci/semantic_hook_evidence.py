"""Strictly derive protocol-v2 semantic hook lifecycle evidence from scoped logs."""

import re
from pathlib import PurePosixPath

from scripts.ci.app_policy import get_policy
from scripts.ci.semantic_protocol import validate_catalog, validate_snapshot
from scripts.ci.semantic_runtime_events import validate_semantic_install_events


THREADTIME = re.compile(
    r'^(?P<timestamp>\d\d-\d\d \d\d:\d\d:\d\d\.\d{3})\s+'
    r'(?P<pid>\d+)\s+(?P<tid>\d+)\s+(?P<priority>[VDIWEF])\s+'
    r'(?P<tag>ImPad|LSPosedFramework):\s+(?P<message>.*)$'
)
MODULE_ENVELOPE = re.compile(
    r'^\((?P<process>[^)]+)\)\[io\.github\.nku100\.impad,ImPad,[^,\]\s]+,\d+,\d+\]\s+(?P<body>.*)$'
)
_HASH = re.compile(r'[a-f0-9]{64}')
_LOADER_CLASS = re.compile(r'[A-Za-z_$][A-Za-z0-9_$.]*')


def derive_semantic_hook_evidence(process_text, logs, candidate, module, installed_module=None):
    app_id, package_name, expected_process, identity, catalog, snapshot, expected_plan = _candidate_parts(candidate)
    expected_ready = 'qfix_ready' if app_id == 'qq' else 'tinker_ready'
    expected_hook_process = f'{package_name}:MSF' if app_id == 'qq' else package_name
    if expected_process != expected_hook_process:
        raise ValueError('Candidate main process does not match the app policy')

    validate_catalog(catalog, app_id)
    if (candidate.get('ruleCatalogSha256') != catalog['ruleCatalogSha256']
            or candidate.get('expectedExecutionSha256') != _hash_value(candidate.get('expectedExecutionSha256'))):
        raise ValueError('Candidate semantic catalog or expected execution identity is invalid')
    validate_snapshot(snapshot, [{'path': item.get('path'), 'sha256': item.get('sha256')}
                                 for item in candidate['apkFiles']])
    dexkit = module.get('dexkit') if isinstance(module, dict) else None
    if not isinstance(dexkit, dict):
        raise ValueError('Module DexKit identity is incomplete')

    processes = _processes(process_text, package_name)
    expected_pids = [pid for pid, name in processes.items() if name == expected_process]
    if len(expected_pids) != 1:
        raise ValueError('Process table must contain exactly one expected main process')
    expected_pid = expected_pids[0]
    parsed = _parse_scoped_logs(logs, processes)
    if not parsed:
        raise ValueError('Scoped module lifecycle log is empty')
    if any(_failure_marker(row['body']) for row in parsed):
        raise ValueError('Scoped module lifecycle log contains an error, incompatibility, or restart-required marker')

    main_rows = [row for row in parsed if row['pid'] == expected_pid]
    by_event = {}
    for row in main_rows:
        event = _event_name(row['body'])
        if event:
            by_event.setdefault(event, []).append(row)
    ready_names = ('tinker_ready', 'qfix_ready')
    ready_rows = [row for name in ready_names for row in by_event.get(name, [])]
    native_rows = by_event.get('native_loaded', [])
    plan_rows = by_event.get('semantic_plan', [])
    hook_rows = by_event.get('hook_installed', [])
    if (len(ready_rows) != 1 or len(native_rows) != 1 or len(plan_rows) != 1 or len(hook_rows) != 1
            or len(by_event.get(expected_ready, [])) != 1
            or any(by_event.get(other) for other in set(ready_names) - {expected_ready})):
        raise ValueError('Expected process must emit exactly one ready, native, semantic-plan and hook event')
    ready, native_row, plan_row, hook_row = ready_rows[0], native_rows[0], plan_rows[0], hook_rows[0]
    if not ready['body'].startswith(f'rule-runtime event={expected_ready} '):
        raise ValueError('Class-loader ready event does not match the app')

    ready_event = _fields(ready['body'], f'rule-runtime event={expected_ready}',
                          {'elapsedRealtimeNs', 'contextPackage', 'loaderClass'})
    native = _fields(native_row['body'], 'rule-runtime event=native_loaded',
                     {'elapsedRealtimeNs', 'apiSha256', 'nativeSha256', 'nativeAbi',
                      'packagingMode', 'sourceApkPath', 'nativeLoadPath'})
    proof = validate_semantic_install_events(
        plan_row['body'], hook_row['body'], expected_plan, catalog, snapshot, expected_process)
    if candidate['expectedExecutionSha256'] != proof['executionSha256']:
        raise ValueError('Runtime execution identity differs from the candidate')
    if (ready_event['contextPackage'] != package_name
            or not _LOADER_CLASS.fullmatch(ready_event['loaderClass'])):
        raise ValueError('Class-loader ready event differs from the expected app')

    abi = identity['abi']
    if (native['apiSha256'] != dexkit.get('dexkitAndroidApiSha256')
            or native['nativeSha256'] != dexkit.get('dexkitAndroidArm64NativeSha256')
            or native['nativeAbi'] != abi
            or native['packagingMode'] not in {'APK_DIRECT', 'EXTRACTED_FILE'}):
        raise ValueError('Native lifecycle identity differs from the verified module')
    if installed_module is not None and native['sourceApkPath'] != installed_module.get('baseApkPath'):
        raise ValueError('Native source APK path differs from the installed module APK')
    if native['packagingMode'] == 'APK_DIRECT':
        expected_load = native['sourceApkPath'] + f'!/lib/{abi}/libdexkit.so'
        if native['nativeLoadPath'] != expected_load:
            raise ValueError('Direct native load path does not identify the module APK entry')
    else:
        load_path = PurePosixPath(native['nativeLoadPath'])
        if not load_path.is_absolute() or load_path.name != 'libdexkit.so' or abi not in load_path.parts:
            raise ValueError('Extracted native load path is not bound to the expected ABI')

    ready_ns = _positive_ns(ready_event['elapsedRealtimeNs'])
    native_ns = _positive_ns(native['elapsedRealtimeNs'])
    if not ready_ns < native_ns < proof['planElapsedRealtimeNs'] < proof['hookElapsedRealtimeNs']:
        raise ValueError('Ready, native, semantic plan and hook lifecycle ordering is invalid')
    if not parsed.index(ready) < parsed.index(native_row) < parsed.index(plan_row) < parsed.index(hook_row):
        raise ValueError('Lifecycle records are not ordered in the scoped log')

    records = [row['line'] for row in parsed if row['pid'] == expected_pid]
    hook_count = proof['hookCount']
    diagnostics = {
        'moduleLoaded': 'OBSERVED',
        'compatibility': 'COMPATIBLE',
        'hookInstallation': f'INSTALLED_{hook_count}',
        'resolvedHooks': [],
        'records': list(dict.fromkeys(records))[-12:],
    }
    ready_evidence = {
        'status': 'READY', 'contextPackage': ready_event['contextPackage'],
        'loaderClass': ready_event['loaderClass'], 'elapsedRealtimeNs': ready_ns,
    }
    lifecycle = {
        ('qfixEvidence' if app_id == 'qq' else 'tinkerEvidence'): ready_evidence,
        'nativeEvidence': {
            'status': 'LOADED', 'apiSha256': native['apiSha256'],
            'nativeSha256': native['nativeSha256'], 'nativeAbi': native['nativeAbi'],
            'packagingMode': native['packagingMode'], 'sourceApkPath': native['sourceApkPath'],
            'nativeLoadPath': native['nativeLoadPath'], 'elapsedRealtimeNs': native_ns,
        },
        'semanticPlanEvidence': {
            'status': 'RESOLVED', 'protocolVersion': 2,
            'executionSha256': proof['executionSha256'], 'snapshotSha256': proof['snapshotSha256'],
            'cacheHit': proof['cacheHit'], 'attempts': proof['attempts'],
            'elapsedRealtimeNs': proof['planElapsedRealtimeNs'],
        },
        'hookEvidence': {
            'status': 'INSTALLED', 'protocolVersion': 2,
            'contractId': proof['completePlan']['contractId'],
            'contractRevision': proof['completePlan']['contractRevision'],
            'strategyId': proof['completePlan']['strategyId'],
            'strategyRevision': proof['completePlan']['strategyRevision'],
            'executionSha256': proof['executionSha256'], 'snapshotSha256': proof['snapshotSha256'],
            'completePlan': proof['completePlan'], 'hookCount': hook_count,
            'processName': proof['processName'],
        },
        'hookCount': hook_count,
        'timingEvidence': {
            'readyBeforeNative': True, 'nativeBeforePlan': True, 'planBeforeHook': True,
            'tinkerBeforeNative': app_id != 'qq', 'qfixBeforeNative': app_id == 'qq',
            'nativeBeforeHook': True, 'compatibleBeforeHook': True,
        },
    }
    return diagnostics, lifecycle


def _candidate_parts(candidate):
    if not isinstance(candidate, dict) or type(candidate.get('schemaVersion')) is not int or candidate['schemaVersion'] != 2:
        raise ValueError('Semantic lifecycle candidate must use schema version 2')
    app_id = candidate.get('appId')
    try:
        package_name = get_policy(app_id).package_name
    except (KeyError, TypeError) as error:
        raise ValueError('Semantic lifecycle candidate has an unknown appId') from error
    identity = candidate.get('identity')
    if not isinstance(identity, dict) or identity.get('packageName') != package_name:
        raise ValueError('Semantic lifecycle candidate identity differs from the app policy')
    expected_process = f'{package_name}:MSF' if app_id == 'qq' else package_name
    if identity.get('abi') != 'arm64-v8a' or type(identity.get('versionCode')) is not int or identity['versionCode'] <= 0:
        raise ValueError('Semantic lifecycle candidate identity is incomplete')
    apk_files = candidate.get('apkFiles')
    if not isinstance(apk_files, list) or not apk_files:
        raise ValueError('Semantic lifecycle candidate has no APK inputs')
    catalog = candidate.get('ruleCatalog')
    snapshot = candidate.get('snapshot')
    expected_plan = candidate.get('expectedPlan')
    if not isinstance(catalog, dict) or not isinstance(snapshot, dict) or not isinstance(expected_plan, dict):
        raise ValueError('Semantic lifecycle candidate is missing its catalog, snapshot, or complete plan')
    return app_id, package_name, expected_process, identity, catalog, snapshot, expected_plan


def _hash_value(value):
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise ValueError('Expected execution SHA-256 is invalid')
    return value


def _processes(process_text, package_name):
    processes = {}
    for line in process_text.splitlines():
        fields = line.split()
        if len(fields) != 2 or not fields[0].isdigit():
            continue
        pid, name = int(fields[0]), fields[1]
        if name == package_name or name.startswith(package_name + ':'):
            if pid in processes and processes[pid] != name:
                raise ValueError('Process table contains a duplicate PID identity')
            processes[pid] = name
    return processes


def _parse_scoped_logs(logs, processes):
    parsed = []
    for line in logs.splitlines():
        if not line:
            continue
        outer = THREADTIME.fullmatch(line)
        if not outer:
            raise ValueError('Scoped module log has an invalid threadtime header')
        envelope = MODULE_ENVELOPE.fullmatch(outer['message'])
        pid = int(outer['pid'])
        if not envelope or pid not in processes or envelope['process'] != processes[pid]:
            raise ValueError('Scoped module log is not bound to its outer process identity')
        parsed.append({'line': line, 'timestamp': outer['timestamp'], 'pid': pid,
                       'process': envelope['process'], 'body': envelope['body']})
    return parsed


def _event_name(body):
    match = re.match(r'rule-runtime event=([a-z_]+)(?:\s|$)', body)
    return match.group(1) if match else None


def _fields(body, prefix, expected):
    if not body.startswith(prefix + ' '):
        raise ValueError('Runtime lifecycle event prefix is invalid')
    fields = {}
    for token in body[len(prefix) + 1:].split():
        key, separator, value = token.partition('=')
        if not separator or not key or not value or key in fields:
            raise ValueError('Runtime lifecycle event contains a malformed or duplicate field')
        fields[key] = value
    if set(fields) != set(expected):
        raise ValueError('Runtime lifecycle event fields do not match the schema')
    return fields


def _failure_marker(body):
    lowered = body.lower()
    return ('restart_required' in lowered or 'incompatible' in lowered
            or re.search(r'(^|\s)(?:error|status=error)(?:\s|:|=|$)', lowered) is not None
            or 'semantic runtime unavailable' in lowered or 'semantic plan rejected' in lowered)


def _positive_ns(value):
    if not re.fullmatch(r'[1-9][0-9]*', value):
        raise ValueError('Invalid lifecycle monotonic timestamp')
    return int(value)
