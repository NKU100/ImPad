"""WeChat-specific UI assertions used by the shared runtime smoke runner."""
import re
import xml.etree.ElementTree as ET
from pathlib import PurePosixPath

hook_installation_log = "installed 2 WeChat hooks"
login_input_activity = "MobileInputUI"
login_navigation_labels = (
    "log in", "login", "log in via mobile number", "log in with phone number",
    "log in on tablet only", "agree", "accept", "allow", "while using the app",
    "登录", "手机号登录", "同意",
)

_THREADTIME = re.compile(
    r'^(?P<timestamp>\d\d-\d\d \d\d:\d\d:\d\d\.\d{3})\s+'
    r'(?P<pid>\d+)\s+(?P<tid>\d+)\s+(?P<priority>[VDIWEF])\s+'
    r'(?P<tag>ImPad|LSPosedFramework):\s+(?P<message>.*)$'
)
_MODULE_ENVELOPE = re.compile(
    r'^\((?P<process>[^)]+)\)\[io\.github\.nku100\.impad,ImPad,[^,\]\s]+,\d+,\d+\]\s+(?P<body>.*)$'
)


def has_app_ui(xml, package_name):
    try:
        nodes = list(ET.fromstring(xml).iter("node"))
    except ET.ParseError:
        return False
    return any(node.get("package") == package_name for node in nodes)


def tablet_entry_visible(text):
    return bool(re.search(r"(?:log(?:ged)?\s*in|登录).*(?:phone\s*&\s*tablet|平板)", text, re.I))


def login_input_ready(activity, xml, package_name):
    return login_input_activity in activity and has_app_ui(xml, package_name)


def is_login_navigation_label(text):
    return text.strip().lower() in login_navigation_labels


def qr_page_ready(activity, xml, package_name, page_activity):
    try:
        nodes = list(ET.fromstring(xml).iter("node"))
    except ET.ParseError:
        return False
    return (page_activity in activity and any(node.get("package") == package_name for node in nodes)
            and any(re.search(r"QR\s*code|二维码", node.get("text", ""), re.I) for node in nodes))


def hook_diagnostics(logs):
    records = [line for line in logs.splitlines()
               if "ImPad" in line and "process skipped:" not in line
               and any(marker in line for marker in ["status=", "waiting for Tinker",
                                                       "resolved tablet=", hook_installation_log])]
    compatible = any("status=COMPATIBLE" in line for line in records)
    installed = any(hook_installation_log in line for line in records)
    descriptors = [line.split("resolved ", 1)[1] for line in records if "resolved tablet=" in line]
    return {
        "moduleLoaded": "OBSERVED" if records else "NOT_OBSERVED",
        "compatibility": "COMPATIBLE" if compatible else "NOT_OBSERVED",
        "hookInstallation": "INSTALLED_2" if installed else "NOT_OBSERVED",
        "resolvedHooks": list(dict.fromkeys(descriptors)),
        "records": list(dict.fromkeys(records))[-12:],
    }


def _strict_nodes(xml):
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as error:
        raise ValueError('Captured UI hierarchy is malformed XML') from error
    if root.tag != 'hierarchy':
        raise ValueError('Captured UI hierarchy has an unexpected root element')
    result = list(root.iter('node'))
    if not result:
        raise ValueError('Captured UI hierarchy has no nodes')
    return result


def _top_resumed_component(activity, package_name):
    rows = re.findall(
        r'^\s*topResumedActivity=ActivityRecord\{[^\n]*?\s+u\d+\s+([^\s}]+)\s+t\d+[^}]*\}',
        activity,
        re.MULTILINE,
    )
    if len(rows) != 1:
        raise ValueError('Activity capture must contain exactly one topResumedActivity')
    component = rows[0]
    if '/' not in component or component.split('/', 1)[0] != package_name:
        raise ValueError('Top resumed activity belongs to a different package')
    return component


def _activity_ends_with(component, simple_name):
    return component.split('/', 1)[1].rsplit('.', 1)[-1] == simple_name


def _app_nodes(xml, package_name):
    result = [node for node in _strict_nodes(xml) if node.get('package') == package_name]
    if not result:
        raise ValueError('Captured UI hierarchy has no nodes owned by the expected package')
    return result


def derive_ui_evidence(samples, package_name, page_activity):
    expected = {'baseline', 'enabled', 'qrCurrent', 'qrConfirmed'}
    if not isinstance(samples, dict) or set(samples) != expected:
        raise ValueError('WeChat UI evidence must contain the four named captures')
    result = {}
    for label in ('baseline', 'enabled'):
        sample = samples[label]
        if not isinstance(sample, dict):
            raise ValueError(f'{label} UI capture is malformed')
        component = _top_resumed_component(sample.get('activity', ''), package_name)
        if not _activity_ends_with(component, login_input_activity):
            raise ValueError(f'{label} capture is not the WeChat login input activity')
        app_nodes = _app_nodes(sample.get('hierarchy', ''), package_name)
        if not any(node.get('visible') != 'false' for node in app_nodes):
            raise ValueError(f'{label} capture has no visible WeChat UI')
        result[label] = {
            'loginPageReady': True,
            'tabletEntry': any(node.get('visible') != 'false' and tablet_entry_visible(node.get('text', ''))
                               for node in app_nodes),
        }
    if result['baseline']['tabletEntry'] or not result['enabled']['tabletEntry']:
        raise ValueError('WeChat tablet entry must appear only in the module-enabled capture')
    qr_components = []
    for label in ('qrCurrent', 'qrConfirmed'):
        sample = samples[label]
        if not isinstance(sample, dict):
            raise ValueError(f'{label} QR capture is malformed')
        component = _top_resumed_component(sample.get('activity', ''), package_name)
        if not _activity_ends_with(component, page_activity):
            raise ValueError(f'{label} capture is not the expected WeChat QR activity')
        app_nodes = _app_nodes(sample.get('hierarchy', ''), package_name)
        if not any(node.get('visible') != 'false'
                   and re.search(r'QR\s*code|二维码', node.get('text', ''), re.IGNORECASE)
                   for node in app_nodes):
            raise ValueError(f'{label} capture has no visible WeChat QR content')
        qr_components.append(component)
    if qr_components[0] != qr_components[1]:
        raise ValueError('The two stable QR samples resolved to different activities')
    result.update(qrPage=page_activity, qrStable=True)
    return result


def derive_manager_state(raw, module_package, app_package):
    if (not isinstance(raw, dict) or set(raw) != {
            'schemaVersion', 'module', 'packageName', 'moduleStateRows', 'scopeRows'}
            or raw.get('schemaVersion') != 1 or raw.get('module') != module_package
            or raw.get('packageName') != app_package):
        raise ValueError('Manager state capture has an invalid schema or package binding')
    module_rows, scope_rows = raw.get('moduleStateRows'), raw.get('scopeRows')
    if not isinstance(module_rows, list) or len(module_rows) != 1 or not isinstance(scope_rows, list) or len(scope_rows) != 1:
        raise ValueError('Manager state capture is missing or ambiguous')
    module_row, scope_row = module_rows[0], scope_rows[0]
    if (not isinstance(module_row, dict)
            or set(module_row) != {'modulePackage', 'userId', 'enabled'}
            or module_row.get('modulePackage') != module_package
            or type(module_row.get('userId')) is not int or module_row['userId'] != 0
            or type(module_row.get('enabled')) is not int or module_row['enabled'] not in (0, 1)):
        raise ValueError('Manager module-state query row is invalid')
    if (not isinstance(scope_row, dict)
            or set(scope_row) != {'modulePackage', 'appPackage', 'userId'}
            or scope_row.get('modulePackage') != module_package
            or scope_row.get('appPackage') != app_package
            or type(scope_row.get('userId')) is not int or scope_row['userId'] != 0):
        raise ValueError('Manager scope query row is invalid')
    return {'moduleEnabled': bool(module_row['enabled']), 'scopeVerified': True}


def derive_installed_identities(raw, app_package, app_sha256, module_package, module_sha256, apk_files=None):
    if not isinstance(raw, dict) or set(raw) != {'schemaVersion', 'app', 'module'} or raw.get('schemaVersion') != 1:
        raise ValueError('Installed APK identity capture has an invalid schema')
    expected = {'app': (app_package, app_sha256), 'module': (module_package, module_sha256)}
    for name, (package_name, digest) in expected.items():
        item = raw.get(name)
        allowed_keys = {'packageName', 'baseApkPath', 'baseApkSha256'}
        if name == 'app' and isinstance(item, dict) and 'splitApks' in item:
            allowed_keys.add('splitApks')
        if (not isinstance(item, dict) or set(item) != allowed_keys
                or item.get('packageName') != package_name or item.get('baseApkSha256') != digest
                or not isinstance(item.get('baseApkPath'), str)
                or not PurePosixPath(item['baseApkPath']).is_absolute()
                or '..' in PurePosixPath(item['baseApkPath']).parts
                or PurePosixPath(item['baseApkPath']).name != 'base.apk'):
            raise ValueError(f'Installed {name} APK identity differs from the verified input')
        if name == 'module' and 'splitApks' in item:
            raise ValueError('Installed module APK must not contain splits')
    _validate_installed_splits(raw['app'], apk_files)
    return raw


def _validate_installed_splits(app, apk_files):
    splits = app.get('splitApks', [])
    if not isinstance(splits, list):
        raise ValueError('Installed app split APK list is invalid')
    paths, hashes = [], []
    for split in splits:
        if (not isinstance(split, dict) or set(split) != {'path', 'sha256'}
                or not isinstance(split['path'], str) or not PurePosixPath(split['path']).is_absolute()
                or '..' in PurePosixPath(split['path']).parts
                or PurePosixPath(split['path']).name in {'', '.', '..'}
                or not re.fullmatch(r'[a-f0-9]{64}', split['sha256'])):
            raise ValueError('Installed app split APK identity is invalid')
        paths.append(split['path'])
        hashes.append(split['sha256'])
    if len(paths) != len(set(paths)) or len(hashes) != len(set(hashes)):
        raise ValueError('Installed app split APK identities are duplicated')
    if apk_files is not None:
        if not isinstance(apk_files, list) or not apk_files:
            raise ValueError('Candidate APK files are invalid')
        expected = [item.get('sha256') for item in apk_files[1:] if isinstance(item, dict)]
        if (len(expected) != len(apk_files) - 1
                or any(not isinstance(digest, str) or not re.fullmatch(r'[a-f0-9]{64}', digest) for digest in expected)
                or len(expected) != len(set(expected)) or set(hashes) != set(expected)):
            raise ValueError('Installed app split APK hashes differ from the verified input')


def scoped_module_log_lines(logcat, process_text, package_name):
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
    if sum(name == package_name for name in processes.values()) != 1:
        raise ValueError('Process table must contain exactly one WeChat main process')
    selected = []
    for line in logcat.splitlines():
        match = _THREADTIME.fullmatch(line)
        if match and match['tag'] in {'ImPad', 'LSPosedFramework'} and int(match['pid']) in processes:
            selected.append(line)
    return '\n'.join(selected) + ('\n' if selected else '')


def _log_fields(body, prefix, expected_keys):
    if not body.startswith(prefix + ' '):
        raise ValueError('Runtime lifecycle event prefix is invalid')
    fields = {}
    for token in body[len(prefix) + 1:].split():
        if '=' not in token:
            raise ValueError('Runtime lifecycle event field is malformed')
        key, value = token.split('=', 1)
        if key in fields:
            raise ValueError('Runtime lifecycle event contains a duplicate field')
        fields[key] = value
    if set(fields) != set(expected_keys):
        raise ValueError('Runtime lifecycle event fields do not match the schema')
    return fields


def derive_hook_evidence(process_text, logs, candidate, module, installed_module=None):
    identity = candidate.get('identity', {})
    plan = candidate.get('expectedPlan', {})
    package_name = identity.get('packageName')
    if (package_name != 'com.tencent.mm' or not isinstance(plan, dict)
            or any(not isinstance(plan.get(key), str)
                   or not re.fullmatch(r'[^/\s]+', plan[key])
                   for key in ('ruleId', 'strategyId'))
            or not isinstance(module.get('dexkit'), dict)):
        raise ValueError('WeChat module lifecycle expectation is incomplete')
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
    main_pids = [pid for pid, name in processes.items() if name == package_name]
    if len(main_pids) != 1:
        raise ValueError('Process table must contain exactly one WeChat main process')
    main_pid = main_pids[0]
    parsed = []
    for line in logs.splitlines():
        if not line:
            continue
        outer = _THREADTIME.fullmatch(line)
        if not outer:
            raise ValueError('Scoped WeChat module log has an invalid threadtime header')
        envelope = _MODULE_ENVELOPE.fullmatch(outer['message'])
        if (not envelope or int(outer['pid']) not in processes
                or envelope['process'] != processes[int(outer['pid'])]):
            raise ValueError('Scoped WeChat module log is not bound to its outer process identity')
        parsed.append({'line': line, 'timestamp': outer['timestamp'], 'pid': int(outer['pid']),
                       'body': envelope['body']})
    if not parsed:
        raise ValueError('Scoped WeChat module log is empty')
    main_rows = lambda predicate: [row for row in parsed if row['pid'] == main_pid and predicate(row['body'])]
    rule_id, strategy_id = plan['ruleId'], plan['strategyId']
    version_code, apk_input_count = identity.get('versionCode'), len(candidate.get('apkFiles', []))
    status_rows = main_rows(lambda body: body.startswith('status='))
    installed_rows = main_rows(lambda body: body.startswith('installed '))
    tinker_rows = main_rows(lambda body: body.startswith('rule-runtime event=tinker_ready '))
    native_rows = main_rows(lambda body: body.startswith('rule-runtime event=native_loaded '))
    hook_rows = main_rows(lambda body: body.startswith('rule-runtime event=hook_installed '))
    if any(len(rows) != 1 for rows in (status_rows, installed_rows, tinker_rows, native_rows, hook_rows)):
        raise ValueError('WeChat main process must emit exactly one compatible, Tinker, native and hook record')
    status = re.fullmatch(
        r'status=COMPATIBLE rule plan=([^/\s]+)/([^\s]+) versionCode=(\d+) apkInputs=(\d+) cacheHit=(true|false)',
        status_rows[0]['body'],
    )
    if (not status or status.group(1) != rule_id or status.group(2) != strategy_id
            or int(status.group(3)) != version_code or int(status.group(4)) != apk_input_count):
        raise ValueError('WeChat compatibility record differs from the prepared candidate plan')
    if installed_rows[0]['body'] != hook_installation_log:
        raise ValueError('WeChat installed-hook count differs from the required target set')
    tinker = _log_fields(tinker_rows[0]['body'], 'rule-runtime event=tinker_ready',
                         {'elapsedRealtimeNs', 'contextPackage', 'loaderClass'})
    native = _log_fields(native_rows[0]['body'], 'rule-runtime event=native_loaded',
                         {'elapsedRealtimeNs', 'apiSha256', 'nativeSha256', 'nativeAbi',
                          'packagingMode', 'sourceApkPath', 'nativeLoadPath'})
    hook = _log_fields(hook_rows[0]['body'], 'rule-runtime event=hook_installed',
                       {'elapsedRealtimeNs', 'ruleId', 'strategyId', 'hookCount', 'processName'})
    if (tinker['contextPackage'] != package_name
            or not re.fullmatch(r'[A-Za-z_$][A-Za-z0-9_$.]*', tinker['loaderClass'])
            or hook['ruleId'] != rule_id or hook['strategyId'] != strategy_id
            or hook['processName'] != package_name or hook['hookCount'] != '2'):
        raise ValueError('WeChat lifecycle events do not match the main package and selected plan')
    dexkit, abi = module['dexkit'], identity.get('abi')
    if (native['apiSha256'] != dexkit.get('dexkitAndroidApiSha256')
            or native['nativeSha256'] != dexkit.get('dexkitAndroidArm64NativeSha256')
            or native['nativeAbi'] != abi or native['packagingMode'] not in {'APK_DIRECT', 'EXTRACTED_FILE'}):
        raise ValueError('WeChat native lifecycle identity differs from the verified module')
    if installed_module is not None and native['sourceApkPath'] != installed_module.get('baseApkPath'):
        raise ValueError('WeChat native source APK path differs from the installed module APK')
    if native['packagingMode'] == 'APK_DIRECT':
        expected_load = native['sourceApkPath'] + f'!/lib/{abi}/libdexkit.so'
        if native['nativeLoadPath'] != expected_load:
            raise ValueError('WeChat direct native load path does not identify the module APK entry')
    else:
        path = PurePosixPath(native['nativeLoadPath'])
        if not path.is_absolute() or path.name != 'libdexkit.so' or abi not in path.parts:
            raise ValueError('WeChat extracted native load path is not bound to the expected ABI')
    times = [tinker['elapsedRealtimeNs'], native['elapsedRealtimeNs'], hook['elapsedRealtimeNs']]
    if (not all(value.isdigit() for value in times)
            or not int(times[0]) < int(times[1]) < int(times[2])
            or status_rows[0]['timestamp'] > hook_rows[0]['timestamp']):
        raise ValueError('WeChat lifecycle event ordering is invalid')
    records = [row['line'] for row in parsed if ('status=COMPATIBLE' in row['body']
               or 'waiting for Tinker' in row['body'] or row['body'].startswith('installed '))]
    diagnostics = {'moduleLoaded': 'OBSERVED', 'compatibility': 'COMPATIBLE',
                   'hookInstallation': 'INSTALLED_2', 'resolvedHooks': [],
                   'records': list(dict.fromkeys(records))[-12:]}
    lifecycle = {
        'tinkerEvidence': {'status': 'READY', 'contextPackage': tinker['contextPackage'],
                           'loaderClass': tinker['loaderClass']},
        'nativeEvidence': {'status': 'LOADED', 'apiSha256': native['apiSha256'],
                           'nativeSha256': native['nativeSha256'], 'nativeAbi': native['nativeAbi'],
                           'packagingMode': native['packagingMode'], 'sourceApkPath': native['sourceApkPath'],
                           'nativeLoadPath': native['nativeLoadPath']},
        'hookEvidence': {'status': 'INSTALLED', 'ruleId': rule_id, 'strategyId': strategy_id,
                         'hookCount': 2, 'processName': package_name},
        'hookCount': 2,
        'timingEvidence': {'tinkerBeforeNative': True, 'nativeBeforeHook': True,
                           'compatibleBeforeHook': True},
    }
    return diagnostics, lifecycle
