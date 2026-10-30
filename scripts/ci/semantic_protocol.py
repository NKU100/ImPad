"""Structural validation and canonical identities for semantic plan protocol v2.

A valid document is internally consistent. Runtime acceptance additionally needs
independent expected targets, exact module/input bytes and business evidence.
"""
import hashlib
import json
import re
import struct

from scripts.ci.dex_descriptors import _validate_target

_SHA256 = re.compile(r'[0-9a-fA-F]{64}')
_PHASES = {'BEFORE', 'AFTER', 'AROUND'}


def _fields(value, keys, label):
    if not isinstance(value, dict) or set(value) != set(keys.split()):
        raise ValueError(f'{label} has an invalid schema')


def _text(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('Expected a nonblank string')
    return value


def _number(value, minimum=0, maximum=2**31-1):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError('Invalid integer')
    return value


def _hash(value):
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError('Invalid SHA-256')
    return value.lower()


def _strings(value, nonempty=False, unique=False):
    if not isinstance(value, list) or (nonempty and not value):
        raise ValueError('Invalid string list')
    for item in value:
        _text(item)
    if unique and len(set(value)) != len(value):
        raise ValueError('Duplicate strings')
    return value


def _configuration(value):
    if not isinstance(value, dict):
        raise ValueError('Invalid callback configuration')
    for key, item in value.items():
        _text(key)
        if not isinstance(item, str):
            raise ValueError('Callback configuration values must be strings')


def _int(value):
    return struct.pack('>i', value)


def _string(value):
    data = value.encode('utf-8')
    return _int(len(data)) + data


def _nullable_string(value):
    return b'\x00' if value is None else b'\x01' + _string(value)


def _jvm_order(value):
    return value.encode('utf-16-be')


def _json_bytes(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')


def catalog_sha256(catalog):
    document = {'schemaVersion': 1, 'appId': catalog['appId'],
        'contractId': catalog['contractId'], 'contractRevision': catalog['contractRevision'],
        'roles': sorted(catalog['requiredRoles'], key=_jvm_order), 'recognizers': catalog['recognizerIds'],
        'strategies': [{'id': strategy['id'], 'revision': strategy['revision'],
            'roles': [{'role': role['role'], 'phase': role['phase'],
                'configuration': dict(sorted(role['configuration'].items(), key=lambda item: _jvm_order(item[0])))} for role in strategy['roles']]}
            for strategy in sorted(catalog['strategies'], key=lambda row: _jvm_order(row['id']))],
        'versionHints': [dict({'recognizer': hint['recognizerId'], 'min': hint['min']},
            **({'max': hint['max']} if hint.get('max') is not None else {})) for hint in catalog['versionHints']]}
    return hashlib.sha256(_json_bytes(document)).hexdigest()


def validate_catalog(catalog, expected_app_id=None):
    _fields(catalog, 'schemaVersion appId ruleCatalogSha256 contractId contractRevision requiredRoles recognizerIds strategies versionHints', 'Semantic catalog')
    if type(catalog['schemaVersion']) is not int or catalog['schemaVersion'] != 2:
        raise ValueError('Unsupported semantic catalog version')
    _text(catalog['appId']); _text(catalog['contractId']); _hash(catalog['ruleCatalogSha256'])
    _number(catalog['contractRevision'], 1)
    if expected_app_id is not None and catalog['appId'] != expected_app_id:
        raise ValueError('Semantic catalog belongs to another app')
    required = _strings(catalog['requiredRoles'], True, True)
    recognizers = _strings(catalog['recognizerIds'], True, True)
    if not isinstance(catalog['strategies'], list) or not catalog['strategies']:
        raise ValueError('Semantic catalog has no strategies')
    strategy_ids = []
    for strategy in catalog['strategies']:
        _fields(strategy, 'id revision roles', 'Strategy')
        strategy_ids.append(_text(strategy['id'])); _number(strategy['revision'], 1)
        if not isinstance(strategy['roles'], list):
            raise ValueError('Invalid strategy roles')
        role_ids = []
        for role in strategy['roles']:
            _fields(role, 'role phase configuration', 'Role')
            role_ids.append(_text(role['role']))
            if not isinstance(role['phase'], str) or role['phase'] not in _PHASES:
                raise ValueError('Invalid hook phase')
            _configuration(role['configuration'])
        if len(set(role_ids)) != len(role_ids) or set(role_ids) != set(required):
            raise ValueError('Strategy roles differ from the complete contract')
    if len(set(strategy_ids)) != len(strategy_ids):
        raise ValueError('Duplicate strategies')
    if not isinstance(catalog['versionHints'], list):
        raise ValueError('Invalid version hints')
    for hint in catalog['versionHints']:
        _fields(hint, 'recognizerId min max', 'Version hint')
        _number(hint['min'], maximum=2**63-1)
        if hint['max'] is not None:
            _number(hint['max'], hint['min'], 2**63-1)
        if hint['recognizerId'] not in recognizers:
            raise ValueError('Version hint refers to an unregistered recognizer')
    if catalog_sha256(catalog) != catalog['ruleCatalogSha256'].lower():
        raise ValueError('Semantic catalog content hash differs')


def snapshot_sha256(snapshot):
    data = _int(snapshot['protocolVersion']) + _string(snapshot['readyLoaderId']) + _int(len(snapshot['loaders']))
    for loader in sorted(snapshot['loaders'], key=lambda row: _jvm_order(row['id'])):
        data += _string(loader['id']) + _nullable_string(loader['parentId']) + _string(loader['delegation']) + _int(len(loader['dexSha256']))
        data += b''.join(_string(value.lower()) for value in loader['dexSha256'])
    return hashlib.sha256(data).hexdigest()


def validate_snapshot(snapshot, input_hashes=None):
    _fields(snapshot, 'protocolVersion readyLoaderId contentSha256 loaders archives', 'Snapshot')
    if type(snapshot['protocolVersion']) is not int or snapshot['protocolVersion'] != 1:
        raise ValueError('Unsupported snapshot protocol')
    _text(snapshot['readyLoaderId']); _hash(snapshot['contentSha256'])
    if not isinstance(snapshot['loaders'], list) or not snapshot['loaders']:
        raise ValueError('Missing snapshot loaders')
    loaders = {}
    for loader in snapshot['loaders']:
        _fields(loader, 'id parentId delegation dexSha256', 'Loader')
        identifier = _text(loader['id'])
        if identifier in loaders:
            raise ValueError('Duplicate loader ID')
        if loader['parentId'] is not None:
            _text(loader['parentId'])
        if not isinstance(loader['delegation'], str) or loader['delegation'] not in {'PARENT_FIRST', 'DELEGATE_LAST'}:
            raise ValueError('Unsupported loader delegation')
        _strings(loader['dexSha256'])
        for digest in loader['dexSha256']:
            _hash(digest)
        loaders[identifier] = loader
    if not any(row['dexSha256'] for row in loaders.values()):
        raise ValueError('Snapshot has no DEX')
    current, seen = snapshot['readyLoaderId'], set()
    while current is not None:
        if current in seen or current not in loaders:
            raise ValueError('Invalid or cyclic loader parent chain')
        seen.add(current); current = loaders[current]['parentId']
    if seen != set(loaders):
        raise ValueError('Snapshot contains disconnected loaders')
    if not isinstance(snapshot['archives'], list):
        raise ValueError('Missing archive manifests')
    archives = {}
    for archive in snapshot['archives']:
        _fields(archive, 'loaderId containerIndex kind containerSha256 dexSha256', 'Archive')
        _text(archive['loaderId'])
        if archive['loaderId'] not in loaders:
            raise ValueError('Archive belongs to an unknown loader')
        index = _number(archive['containerIndex'])
        position = archive['loaderId'], index
        if position in archives:
            raise ValueError('Duplicate container position')
        if not isinstance(archive['kind'], str) or archive['kind'] not in {'ZIP', 'DEX'}:
            raise ValueError('Unknown container kind')
        content_hash = _hash(archive['containerSha256'])
        _strings(archive['dexSha256'])
        hashes = [_hash(value) for value in archive['dexSha256']]
        if archive['kind'] == 'DEX' and hashes != [content_hash]:
            raise ValueError('Raw DEX manifest differs from its container')
        archives[position] = archive
    for loader_id, loader in loaders.items():
        entries = sorted((row for (owner, _), row in archives.items() if owner == loader_id), key=lambda row: row['containerIndex'])
        if [row['containerIndex'] for row in entries] != list(range(len(entries))):
            raise ValueError('Container manifest positions are incomplete')
        if [_hash(value) for row in entries for value in row['dexSha256']] != [_hash(value) for value in loader['dexSha256']]:
            raise ValueError('Container manifests do not reconstruct the snapshot')
    if snapshot_sha256(snapshot) != snapshot['contentSha256'].lower():
        raise ValueError('Snapshot content identity differs')
    if input_hashes is not None:
        if (not isinstance(input_hashes, list) or not input_hashes or len(loaders) != 1
                or loaders[snapshot['readyLoaderId']]['delegation'] != 'PARENT_FIRST'):
            raise ValueError('Static APK inputs require one parent-first loader')
        entries = sorted(archives.values(), key=lambda row: row['containerIndex'])
        for row in input_hashes:
            _fields(row, 'path sha256', 'APK input'); _text(row['path']); _hash(row['sha256'])
        if [row['sha256'].lower() for row in input_hashes] != [row['containerSha256'].lower() for row in entries]:
            raise ValueError('APK input hashes differ from captured manifests')


def _plan_bytes(plan):
    _fields(plan, 'schemaVersion snapshotSha256 contractId contractRevision strategyId strategyRevision hooks', 'Complete plan')
    if type(plan['schemaVersion']) is not int or plan['schemaVersion'] != 1:
        raise ValueError('Unsupported complete plan schema')
    _hash(plan['snapshotSha256']); _text(plan['contractId']); _text(plan['strategyId'])
    _number(plan['contractRevision'], 1); _number(plan['strategyRevision'], 1)
    if not isinstance(plan['hooks'], list) or not plan['hooks']:
        raise ValueError('Complete plan has no hooks')
    data = _int(1) + _string(plan['snapshotSha256'].lower()) + _string(plan['contractId']) + _int(plan['contractRevision']) + _string(plan['strategyId']) + _int(plan['strategyRevision']) + _int(len(plan['hooks']))
    roles = []
    for hook in plan['hooks']:
        _fields(hook, 'role descriptor shape source phase configuration', 'Plan hook')
        roles.append(_text(hook['role']))
        _validate_target(hook['role'], {'descriptor': hook['descriptor'], 'shape': hook['shape']})
        source = hook['source']
        _fields(source, 'loaderId dexIndex dexSha256', 'Target source')
        _text(source['loaderId']); _number(source['dexIndex']); _hash(source['dexSha256'])
        if not isinstance(hook['phase'], str) or hook['phase'] not in _PHASES:
            raise ValueError('Invalid plan hook phase')
        _configuration(hook['configuration'])
        shape = hook['shape']
        data += _string(hook['role']) + _string(hook['descriptor']) + _int(len(shape['parameters']))
        data += b''.join(_string(value) for value in shape['parameters'])
        data += _string(shape['returnType']) + bytes([shape['isStatic']])
        data += _string(source['loaderId']) + _int(source['dexIndex']) + _string(source['dexSha256'].lower()) + _string(hook['phase'])
        data += _int(len(hook['configuration']))
        for key, value in sorted(hook['configuration'].items(), key=lambda item: _jvm_order(item[0])):
            data += _string(key) + _string(value)
    if len(set(roles)) != len(roles):
        raise ValueError('Duplicate complete-plan roles')
    return data


def plan_execution_sha256(plan):
    return hashlib.sha256(_plan_bytes(plan)).hexdigest()


def validate_complete_plan(plan, catalog, snapshot):
    data = _plan_bytes(plan)
    validate_catalog(catalog)
    validate_snapshot(snapshot)
    if (plan['snapshotSha256'].lower() != snapshot['contentSha256'].lower()
            or plan['contractId'] != catalog['contractId'] or plan['contractRevision'] != catalog['contractRevision']):
        raise ValueError('Complete plan belongs to another snapshot or behavior contract')
    strategy = next((row for row in catalog['strategies'] if row['id'] == plan['strategyId'] and row['revision'] == plan['strategyRevision']), None)
    if strategy is None or [row['role'] for row in plan['hooks']] != [row['role'] for row in strategy['roles']]:
        raise ValueError('Complete plan strategy or ordered roles differ')
    loaders = {row['id']: row for row in snapshot['loaders']}
    for hook, role in zip(plan['hooks'], strategy['roles']):
        if hook['phase'] != role['phase'] or hook['configuration'] != role['configuration']:
            raise ValueError('Complete plan callback behavior differs')
        source = hook['source']
        hashes = loaders.get(source['loaderId'], {}).get('dexSha256', [])
        if source['dexIndex'] >= len(hashes) or hashes[source['dexIndex']].lower() != source['dexSha256'].lower():
            raise ValueError('Hook source is outside the snapshot')
    return hashlib.sha256(data).hexdigest()


_EXECUTION_FIELDS = {
    'ruleCatalogSha256', 'ruleImplementationSha256', 'sharedResolverImplementationSha256',
    'dexkitLockSha256', 'dexkitSourceCommit', 'dexkitApiSha256', 'dexkitNativeSha256',
    'dexkitPlatform', 'dexkitAndroidApiSha256', 'dexkitLinuxAarch64NativeSha256',
    'dexkitLinuxX8664NativeSha256', 'dexkitAndroidArm64NativeSha256',
    'strategyImplementationSha256', 'cacheIdentitySha256', 'sourceTreeSha256',
    'routeConfigurationSha256', 'dependencyLockSha256', 'fingerprintFileSha256',
}


def execution_identity_sha256(identity):
    fields = [('catalog', 'ruleCatalogSha256'), ('rules', 'ruleImplementationSha256'),
        ('resolver', 'sharedResolverImplementationSha256'), ('strategy', 'strategyImplementationSha256'),
        ('sourceTree', 'sourceTreeSha256'), ('route', 'routeConfigurationSha256'),
        ('dependencies', 'dependencyLockSha256'), ('lock', 'dexkitLockSha256'),
        ('source', 'dexkitSourceCommit'), ('api', 'dexkitApiSha256'),
        ('androidApi', 'dexkitAndroidApiSha256'), ('linuxAarch64', 'dexkitLinuxAarch64NativeSha256'),
        ('linuxX8664', 'dexkitLinuxX8664NativeSha256'), ('androidArm64', 'dexkitAndroidArm64NativeSha256'),
        ('native', 'dexkitNativeSha256'), ('platform', 'dexkitPlatform')]
    return hashlib.sha256(''.join(f'{label}={identity[key] or ""}\n' for label, key in fields).encode('utf-8')).hexdigest()


def _validate_execution_identity(identity, catalog):
    if not isinstance(identity, dict) or set(identity) != _EXECUTION_FIELDS:
        raise ValueError('Incomplete execution identity')
    for key in _EXECUTION_FIELDS - {'dexkitPlatform', 'dexkitSourceCommit', 'dexkitNativeSha256'}:
        _hash(identity[key])
    if not isinstance(identity['dexkitSourceCommit'], str) or not re.fullmatch(r'[0-9a-f]{40}', identity['dexkitSourceCommit']):
        raise ValueError('Invalid DexKit source commit')
    if identity['ruleCatalogSha256'] != catalog['ruleCatalogSha256']:
        raise ValueError('Execution identity belongs to another catalog')
    if identity['dexkitNativeSha256'] is not None:
        _hash(identity['dexkitNativeSha256'])
    expected_native = {'linux-aarch64': 'dexkitLinuxAarch64NativeSha256', 'linux-x86_64': 'dexkitLinuxX8664NativeSha256'}
    platform = identity['dexkitPlatform']
    if platform is not None:
        if not isinstance(platform, str) or platform not in expected_native or identity['dexkitNativeSha256'] != identity[expected_native[platform]]:
            raise ValueError('Loaded native identity differs from its platform lock')
    elif identity['dexkitNativeSha256'] is not None:
        raise ValueError('Native identity has no platform')
    if identity['cacheIdentitySha256'] != execution_identity_sha256(identity):
        raise ValueError('Execution cache identity is inconsistent')


def _evidence_map(value):
    if not isinstance(value, dict):
        raise ValueError('Invalid recognizer evidence map')
    for key, evidence in value.items():
        _text(key); _strings(evidence)
    return value


def _diagnostics_map(value):
    if not isinstance(value, dict):
        raise ValueError('Invalid diagnostic map')
    for key, messages in value.items():
        _text(key)
        if not isinstance(messages, list) or not all(isinstance(item, str) for item in messages):
            raise ValueError('Invalid diagnostic messages')
    return value


def _validate_plan_envelope(row, catalog, snapshot, evidence_key):
    _fields(row, f'plan executionSha256 {evidence_key}', 'Plan evidence')
    digest = validate_complete_plan(row['plan'], catalog, snapshot)
    if _hash(row['executionSha256']) != digest:
        raise ValueError('Plan execution digest differs from its full execution fields')
    if evidence_key == 'evidence':
        _strings(row['evidence'])
    else:
        _evidence_map(row['evidenceByRecognizer'])
    return _plan_bytes(row['plan'])


def _expected_selection(recognizers):
    empty = {'status': 'NO_MATCH', 'plan': None, 'executionSha256': None,
        'evidenceByRecognizer': {}, 'alternatives': [], 'diagnostics': {}, 'incompleteReasons': {}}
    for status in ('EVALUATION_ERROR', 'UNSUPPORTED_INPUT'):
        diagnostics = {row['recognizerId']: row['diagnostics'] for row in recognizers if row['status'] == status}
        if diagnostics:
            return dict(empty, status=status, diagnostics=diagnostics)
    matches = {}
    for row in sorted(recognizers, key=lambda row: _jvm_order(row['recognizerId'])):
        for entry in row['plans']:
            key = _plan_bytes(entry['plan'])
            match = matches.setdefault(key, {'plan': entry['plan'], 'executionSha256': plan_execution_sha256(entry['plan']), 'evidenceByRecognizer': {}})
            match['evidenceByRecognizer'].setdefault(row['recognizerId'], []).extend(entry['evidence'])
    ordered = sorted(matches.values(), key=lambda row: row['executionSha256'])
    for match in ordered:
        match['evidenceByRecognizer'] = {key: sorted(set(evidence), key=_jvm_order) for key, evidence in match['evidenceByRecognizer'].items()}
    if len(ordered) > 1:
        return dict(empty, status='CONFLICT', alternatives=ordered)
    incomplete = {row['recognizerId']: row['reason'] for row in recognizers if row['status'] == 'INCOMPLETE'}
    if incomplete:
        return dict(empty, status='INCOMPLETE', alternatives=ordered, incompleteReasons=incomplete)
    return dict(empty, status='SELECTED', **ordered[0]) if ordered else empty


def _selection_identity(selection):
    return (selection['status'], _plan_bytes(selection['plan']) if selection['plan'] is not None else None,
        selection['executionSha256'].lower() if selection['executionSha256'] is not None else None,
        selection['evidenceByRecognizer'],
        [(_plan_bytes(row['plan']), row['executionSha256'].lower(), row['evidenceByRecognizer']) for row in selection['alternatives']],
        selection['diagnostics'], selection['incompleteReasons'])


def validate_check_report(report, expected_app_id=None, expected_identity=None, trusted_catalog=None):
    """Validate internal consistency; callers still need independent acceptance."""
    from scripts.ci.app_policy import get_policy
    _fields(report, 'schemaVersion analysisScope appId identity inputHashes ruleCatalog executionIdentity snapshot evaluatedRecognizerIds recognizers selection', 'Semantic checker report')
    if type(report['schemaVersion']) is not int or report['schemaVersion'] != 2 or report['analysisScope'] != 'APK_FILE_SNAPSHOT':
        raise ValueError('Unsupported semantic checker scope or protocol')
    if expected_app_id is not None and report['appId'] != expected_app_id:
        raise ValueError('Checker report belongs to another app')
    policy = get_policy(report['appId'])
    identity = report['identity']
    _fields(identity, 'packageName versionName versionCode abi apkSha256 signerSha256', 'APK identity')
    if (identity['packageName'] != policy.package_name or identity['abi'] not in policy.supported_abis
            or _hash(identity['signerSha256']) != policy.signer_sha256 or not isinstance(identity['versionName'], str)):
        raise ValueError('APK identity does not match official application policy')
    _number(identity['versionCode'], 1, 2**63-1); _hash(identity['apkSha256'])
    if expected_identity is not None and identity != expected_identity:
        raise ValueError('Checker report identity differs from the independently verified input')
    catalog, snapshot = report['ruleCatalog'], report['snapshot']
    validate_catalog(catalog, report['appId'])
    if trusted_catalog is not None and catalog != trusted_catalog:
        raise ValueError('Checker catalog differs from independently generated catalog')
    validate_snapshot(snapshot, report['inputHashes'])
    if identity['apkSha256'].lower() != report['inputHashes'][0]['sha256'].lower():
        raise ValueError('APK identity does not match the exact base bytes')
    _validate_execution_identity(report['executionIdentity'], catalog)
    evaluated = _strings(report['evaluatedRecognizerIds'], unique=True)
    preferred = {row['recognizerId'] for row in catalog['versionHints'] if identity['versionCode'] >= row['min']
        and (row['max'] is None or identity['versionCode'] <= row['max'])}
    order = [key for key in catalog['recognizerIds'] if key in preferred] + [key for key in catalog['recognizerIds'] if key not in preferred]
    if evaluated != order[:len(evaluated)]:
        raise ValueError('Evaluated recognizers differ from the complete hinted query order')
    recognizers = report['recognizers']
    if not isinstance(recognizers, list) or [row.get('recognizerId') if isinstance(row, dict) else None for row in recognizers] != catalog['recognizerIds']:
        raise ValueError('Report must include every registered recognizer exactly once')
    for row in recognizers:
        _fields(row, 'recognizerId status plans diagnostics reason', 'Recognizer result')
        if not isinstance(row['status'], str) or row['status'] not in {'NO_MATCH', 'PLANS', 'INCOMPLETE', 'UNSUPPORTED_INPUT', 'EVALUATION_ERROR'}:
            raise ValueError('Unknown recognizer result')
        if not isinstance(row['plans'], list) or not isinstance(row['diagnostics'], list) or not all(isinstance(item, str) for item in row['diagnostics']):
            raise ValueError('Invalid recognizer plans or diagnostics')
        if row['status'] == 'PLANS' and not row['plans']:
            raise ValueError('Plans result has no complete plan')
        if row['status'] not in {'PLANS', 'INCOMPLETE'} and row['plans']:
            raise ValueError('Result status contradicts complete plan evidence')
        if row['status'] == 'INCOMPLETE':
            _text(row['reason'])
        elif row['reason'] is not None:
            raise ValueError('Completed result has an incomplete reason')
        if row['recognizerId'] not in evaluated and row['status'] != 'INCOMPLETE':
            raise ValueError('Unevaluated recognizer must explicitly report incomplete')
        for entry in row['plans']:
            _validate_plan_envelope(entry, catalog, snapshot, 'evidence')
    selection = report['selection']
    _fields(selection, 'status plan executionSha256 evidenceByRecognizer alternatives diagnostics incompleteReasons', 'Complete selection')
    _evidence_map(selection['evidenceByRecognizer']); _diagnostics_map(selection['diagnostics'])
    if not isinstance(selection['incompleteReasons'], dict) or not isinstance(selection['alternatives'], list):
        raise ValueError('Invalid complete selection evidence')
    for key, reason in selection['incompleteReasons'].items():
        _text(key); _text(reason)
    if selection['plan'] is not None:
        digest = validate_complete_plan(selection['plan'], catalog, snapshot)
        if _hash(selection['executionSha256']) != digest:
            raise ValueError('Selected execution digest differs')
    elif selection['executionSha256'] is not None:
        raise ValueError('Execution digest has no selected complete plan')
    for entry in selection['alternatives']:
        _validate_plan_envelope(entry, catalog, snapshot, 'evidenceByRecognizer')
    if _selection_identity(selection) != _selection_identity(_expected_selection(recognizers)):
        raise ValueError('Selection contradicts the strict aggregate of all recognizer results')
