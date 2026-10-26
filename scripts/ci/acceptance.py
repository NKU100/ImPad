"""Read and validate immutable, runtime-verified APK acceptance records."""
from datetime import datetime, timezone
from dataclasses import dataclass
import json
import hashlib
import re
import shlex
import subprocess
import tempfile
import uuid
from pathlib import Path

from scripts.ci.app_policy import get_policy
from scripts.ci.discover_latest_wechat import validate_official_apk_url
from scripts.ci.download_url import validate_download_url

SCHEMA_VERSION = 1
_DIGEST = re.compile(r"[a-f0-9]{64}")
_CATALOG_CACHE = {}


class RoutingReviewRequired(ValueError):
    """The candidate is not strictly newer than all accepted routing evidence."""

    status = 'ROUTING_REVIEW_REQUIRED'

    def __init__(self):
        super().__init__(self.status)


@dataclass(frozen=True)
class AcceptanceSnapshot:
    path: Path
    raw_bytes: bytes | None
    sha256: str | None
    records_json: bytes
    initial_migration: bool

    @property
    def records(self) -> list[dict]:
        return json.loads(self.records_json)


def read_acceptance_snapshot(path: Path, app_id: str, initial_migration: bool,
                             catalog: dict | None = None,
                             repository: Path | None = None) -> AcceptanceSnapshot:
    path = Path(path).resolve()
    try:
        raw_bytes = path.read_bytes()
    except FileNotFoundError:
        if initial_migration:
            return AcceptanceSnapshot(path, None, None, b'[]', True)
        raise FileNotFoundError(f"Acceptance table is missing: {path}")
    except OSError as error:
        raise ValueError(f"Acceptance table is unreadable: {path}") from error
    records = load_records_bytes(raw_bytes, app_id, initial_migration,
                                 catalog=catalog, repository=repository, source_path=path)
    return AcceptanceSnapshot(path, raw_bytes, hashlib.sha256(raw_bytes).hexdigest(),
                              json.dumps(records, separators=(',', ':')).encode(),
                              bool(initial_migration))


def validate_acceptance_snapshot(snapshot: AcceptanceSnapshot, app_id: str,
                                 initial_migration: bool, catalog: dict | None = None,
                                 repository: Path | None = None) -> list[dict]:
    if (not isinstance(snapshot, AcceptanceSnapshot)
            or snapshot.initial_migration is not bool(initial_migration)):
        raise ValueError('Acceptance snapshot object is invalid for this migration mode')
    if snapshot.raw_bytes is None:
        if not initial_migration or snapshot.sha256 is not None:
            raise ValueError('Only an initial migration may use a missing acceptance snapshot')
        records = []
    else:
        digest = hashlib.sha256(snapshot.raw_bytes).hexdigest()
        if digest != snapshot.sha256:
            raise ValueError('Acceptance snapshot digest does not match its immutable bytes')
        records = load_records_bytes(snapshot.raw_bytes, app_id, initial_migration,
                                     catalog=catalog, repository=repository,
                                     source_path=snapshot.path)
    serialized = json.dumps(records, separators=(',', ':')).encode()
    if serialized != snapshot.records_json:
        raise ValueError('Acceptance snapshot records differ from its validated bytes')
    return records


def load_rule_catalog(app_id: str, repository: Path | None = None) -> dict:
    repository = Path(repository or Path.cwd()).resolve()
    key = (app_id, str(repository))
    if key in _CATALOG_CACHE:
        return _CATALOG_CACHE[key]
    with tempfile.TemporaryDirectory(prefix='impad-rule-catalog-') as temporary:
        report = Path(temporary) / 'catalog.json'
        arguments = shlex.join(['catalog', '--app-id', app_id, '--report', str(report)])
        subprocess.run(['./gradlew', '--no-daemon', ':compat-checker:run', '--args=' + arguments],
                       cwd=repository, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                       check=True, timeout=300)
        catalog = json.loads(report.read_text())
    if (not isinstance(catalog, dict) or catalog.get('schemaVersion') != 1
            or catalog.get('appId') != app_id
            or not _DIGEST.fullmatch(catalog.get('ruleCatalogSha256', ''))
            or not isinstance(catalog.get('definitions'), list)
            or not isinstance(catalog.get('bindings'), list)
            or not isinstance(catalog.get('strategyIds'), list)):
        raise ValueError('Shared checker returned an invalid rule catalog')
    _CATALOG_CACHE[key] = catalog
    return catalog


def _dex_type_tokens(value: str, allow_void: bool) -> list[str]:
    tokens = []
    index = 0
    primitive = set('BCDFIJSZ') | ({'V'} if allow_void else set())
    while index < len(value):
        start = index
        while index < len(value) and value[index] == '[':
            index += 1
        if index >= len(value):
            raise ValueError('Method descriptor has an incomplete array type')
        code = value[index]
        if code == 'L':
            end = value.find(';', index)
            owner = value[index + 1:end] if end >= 0 else ''
            if (end < 0 or not owner or any(c in owner for c in '.[];')
                    or owner.startswith('/') or owner.endswith('/') or '//' in owner):
                raise ValueError('Method descriptor has an invalid object type')
            index = end + 1
        elif code in primitive:
            index += 1
            if code == 'V' and index - start != 1:
                raise ValueError('Void cannot be an array type')
        else:
            raise ValueError('Method descriptor has an invalid type')
        tokens.append(value[start:index])
    return tokens


def _java_type_descriptor(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError('Method shape has an empty type')
    dimensions = 0
    while value.endswith('[]'):
        dimensions += 1
        value = value[:-2]
    primitive = {'void': 'V', 'boolean': 'Z', 'byte': 'B', 'char': 'C', 'short': 'S',
                 'int': 'I', 'long': 'J', 'float': 'F', 'double': 'D'}
    code = primitive.get(value)
    if code is None:
        if not re.fullmatch(r'[A-Za-z_$][A-Za-z0-9_$.]*', value):
            raise ValueError('Method shape has an invalid Java type')
        code = 'L' + value.replace('.', '/') + ';'
    if code == 'V' and dimensions:
        raise ValueError('Void cannot be an array type')
    return '[' * dimensions + code


def _validate_target(target_id: str, target: dict) -> None:
    if not isinstance(target, dict) or set(target) != {'descriptor', 'shape'}:
        raise ValueError(f'Expected target {target_id} has an incomplete descriptor')
    descriptor = target.get('descriptor')
    match = re.fullmatch(r'(L[^;]+;)->([^\s(;]+)\(([^)]*)\)(.+)', descriptor or '')
    if not match:
        raise ValueError(f'Expected target {target_id} has an invalid method descriptor')
    owner, method_name = match.group(1)[1:-1], match.group(2)
    if (owner.startswith('/') or owner.endswith('/') or '//' in owner
            or any(character in owner for character in '.[];')
            or any(character in method_name for character in '.[/;')):
        raise ValueError(f'Expected target {target_id} has an invalid method descriptor')
    params = _dex_type_tokens(match.group(3), allow_void=False)
    result = _dex_type_tokens(match.group(4), allow_void=True)
    if len(result) != 1:
        raise ValueError(f'Expected target {target_id} has an invalid return descriptor')
    shape = target.get('shape')
    if (not isinstance(shape, dict) or set(shape) != {'parameters', 'returnType', 'isStatic'}
            or not isinstance(shape.get('parameters'), list)
            or type(shape.get('isStatic')) is not bool):
        raise ValueError(f'Expected target {target_id} has an incomplete method shape')
    try:
        shape_params = [_java_type_descriptor(item) for item in shape['parameters']]
        shape_return = _java_type_descriptor(shape.get('returnType'))
    except (TypeError, ValueError) as error:
        raise ValueError(f'Expected target {target_id} has an invalid method shape') from error
    if params != shape_params or result[0] != shape_return:
        raise ValueError(f'Expected target {target_id} descriptor and shape do not match')


def validate_expected_plan(plan: dict, catalog: dict, version_code: int | None = None) -> None:
    if not isinstance(plan, dict) or set(plan) != {'selection', 'ruleId', 'strategyId', 'targets'}:
        raise ValueError('Expected plan has an invalid schema')
    if (plan.get('selection') != 'UNIQUE' or not isinstance(catalog, dict)
            or catalog.get('schemaVersion') != 1
            or not _DIGEST.fullmatch(catalog.get('ruleCatalogSha256', ''))):
        raise ValueError('Expected plan or rule catalog schema is invalid')
    definitions = catalog.get('definitions')
    strategies = catalog.get('strategyIds')
    if (not isinstance(definitions, list) or not isinstance(strategies, list)
            or any(not isinstance(value, str) or not value for value in strategies)
            or len(set(strategies)) != len(strategies)):
        raise ValueError('Expected plan rule catalog is incomplete')
    matching_definitions = [row for row in definitions
                            if isinstance(row, dict) and row.get('ruleId') == plan.get('ruleId')]
    definition = matching_definitions[0] if len(matching_definitions) == 1 else None
    if (definition is None or set(definition) != {'ruleId', 'strategyId', 'requiredTargetIds'}
            or definition.get('strategyId') != plan.get('strategyId')
            or plan.get('strategyId') not in strategies):
        raise ValueError('Expected plan rule or strategy does not match the shared catalog')
    required = definition.get('requiredTargetIds')
    targets = plan.get('targets')
    if (not isinstance(required, list) or not required or len(set(required)) != len(required)
            or not isinstance(targets, dict) or set(targets) != set(required)):
        raise ValueError('Expected plan targets do not match the shared catalog')
    for target_id, target in targets.items():
        _validate_target(target_id, target)
    if version_code is not None:
        bindings = catalog.get('bindings')
        if type(version_code) is not int or version_code <= 0 or not isinstance(bindings, list):
            raise ValueError('Expected plan candidate version or rule bindings are invalid')
        matching_bindings = [row for row in bindings
                             if isinstance(row, dict) and row.get('ruleId') == plan['ruleId']]
        valid_bindings = [row for row in matching_bindings
                          if set(row) == {'ruleId', 'min', 'max'}
                          and type(row.get('min')) is int and row['min'] > 0
                          and (row.get('max') is None
                               or type(row.get('max')) is int and row['max'] >= row['min'])]
        if (len(valid_bindings) != len(matching_bindings)
                or not any(row['min'] <= version_code
                           and (row['max'] is None or version_code <= row['max'])
                           for row in valid_bindings)):
            raise ValueError('Expected plan rule has no valid version binding in the shared catalog')


def derive_record_order(records: list[dict], rule_id: str, record_id: str,
                        apk_sha256: str | None = None) -> dict:
    groups = {}
    seen_orders = {}
    seen_digests = set()
    for record in records:
        saved_rule = record.get('expectedPlan', {}).get('ruleId')
        order = record.get('ruleOrder')
        first_id = record.get('firstRecordId')
        if (not isinstance(saved_rule, str) or type(order) is not int or order < 1
                or not isinstance(first_id, str) or not first_id):
            raise ValueError('Acceptance snapshot has malformed rule order history')
        if saved_rule in seen_orders and seen_orders[saved_rule] != order:
            raise ValueError('Acceptance snapshot has inconsistent rule order history')
        if order in seen_orders.values() and seen_orders.get(saved_rule) != order:
            raise ValueError('Acceptance snapshot reuses a rule order')
        seen_orders[saved_rule] = order
        group = groups.setdefault(saved_rule, [])
        group.append(record)
        digest = record.get('identity', {}).get('apkSha256', '').lower()
        if digest:
            seen_digests.add(digest)
    if apk_sha256 and apk_sha256.lower() in seen_digests:
        raise ValueError('Candidate APK SHA-256 is already present in the validated acceptance snapshot')
    if sorted(seen_orders.values()) != list(range(1, len(seen_orders) + 1)):
        raise ValueError('Acceptance snapshot rule order is not contiguous')
    for saved_rule, group in groups.items():
        first = group[0]
        if (first.get('firstForRule') is not True or first.get('firstRecordId') != first.get('recordId')
                or any(row.get('firstForRule') is not False
                       or row.get('firstRecordId') != first.get('recordId') for row in group[1:])):
            raise ValueError(f'Acceptance snapshot fixed first record is malformed for {saved_rule}')
    if rule_id in groups:
        group = groups[rule_id]
        first = group[0]
        expected_order = seen_orders[rule_id]
        if any(row.get('firstRecordId') != first.get('recordId') for row in group):
            raise ValueError(f'Acceptance snapshot fixed first record is inconsistent for {rule_id}')
        return {'firstForRule': False, 'firstRecordId': first['recordId'], 'ruleOrder': expected_order}
    return {'firstForRule': True, 'firstRecordId': record_id,
            'ruleOrder': max(seen_orders.values(), default=0) + 1}


def propose_forward_binding(bindings: list[dict], records: list[dict],
                            candidate_version: int, new_rule_id: str) -> list[dict]:
    """Propose a forward-only route from validated history without mutating it."""
    if (type(candidate_version) is not int or candidate_version <= 0
            or not isinstance(new_rule_id, str)
            or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_.-]*', new_rule_id)):
        raise ValueError('Candidate version or new rule ID is invalid')
    if not isinstance(bindings, list) or not isinstance(records, list):
        raise ValueError('Bindings and acceptance records must be lists')

    proposed = []
    seen_rules = set()
    for row in bindings:
        if (not isinstance(row, dict) or set(row) != {'ruleId', 'min', 'max'}
                or not isinstance(row.get('ruleId'), str) or not row['ruleId']
                or type(row.get('min')) is not int or row['min'] <= 0
                or (row.get('max') is not None
                    and (type(row['max']) is not int or row['max'] < row['min']))):
            raise ValueError('Existing rule binding is malformed')
        proposed.append(dict(row))
        seen_rules.add(row['ruleId'])
    if new_rule_id in seen_rules:
        raise ValueError(f'New rule ID already exists: {new_rule_id}')

    ordered = sorted(proposed, key=lambda row: (row['min'], row['max'] is None,
                                                 row['max'] if row['max'] is not None else 0))
    for previous, current in zip(ordered, ordered[1:]):
        if previous['max'] is None or previous['max'] >= current['min']:
            raise ValueError('Existing rule bindings overlap')

    accepted_max = {}
    all_accepted = []
    for record in records:
        if not isinstance(record, dict):
            raise ValueError('Acceptance record history is malformed')
        rule_id = record.get('expectedPlan', {}).get('ruleId')
        version = record.get('identity', {}).get('versionCode')
        if (not isinstance(rule_id, str) or type(version) is not int or version <= 0
                or rule_id not in seen_rules):
            raise ValueError('Acceptance record is not bound to an existing rule and version')
        accepted_max[rule_id] = max(accepted_max.get(rule_id, 0), version)
        all_accepted.append(version)
    if all_accepted and candidate_version <= max(all_accepted):
        raise RoutingReviewRequired()
    if not proposed and records:
        raise ValueError('Acceptance history exists without rule bindings')

    for row in proposed:
        if row['max'] is not None and row['max'] < candidate_version:
            continue
        validated_max = accepted_max.get(row['ruleId'])
        if validated_max is None or validated_max < row['min'] or validated_max >= candidate_version:
            raise RoutingReviewRequired()
        row['max'] = validated_max

    proposed.append({'ruleId': new_rule_id, 'min': candidate_version, 'max': None})
    proposed.sort(key=lambda row: (row['min'], row['max'] is None,
                                   row['max'] if row['max'] is not None else 0))
    for previous, current in zip(proposed, proposed[1:]):
        if previous['max'] is None or previous['max'] >= current['min']:
            raise ValueError('Proposed forward bindings overlap')
    return proposed


def load_records(path: Path, app_id: str, initial_migration: bool,
                 catalog: dict | None = None, repository: Path | None = None) -> list[dict]:
    path = Path(path)
    try:
        raw_bytes = path.read_bytes()
    except FileNotFoundError:
        if initial_migration:
            return []
        raise FileNotFoundError(f"Acceptance table is missing: {path}")
    except OSError as error:
        raise ValueError(f"Acceptance table is unreadable: {path}") from error
    return load_records_bytes(raw_bytes, app_id, initial_migration,
                              catalog=catalog, repository=repository, source_path=path)


def load_records_bytes(raw_bytes: bytes, app_id: str, initial_migration: bool,
                       catalog: dict | None = None, repository: Path | None = None,
                       source_path: Path | str = '<bytes>') -> list[dict]:
    if not isinstance(raw_bytes, bytes):
        raise TypeError('Acceptance snapshot must be immutable bytes')
    try:
        document = json.loads(raw_bytes.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"Acceptance table is unreadable: {source_path}") from error
    if (not isinstance(document, dict) or document.get("schemaVersion") != SCHEMA_VERSION
            or document.get("appId") != app_id or not isinstance(document.get("records"), list)):
        raise ValueError("Acceptance table schema or appId is invalid")
    records = document["records"]
    if not records and not initial_migration:
        raise ValueError("Acceptance table is unexpectedly empty")
    policy = get_policy(app_id)
    seen_ids = set()
    first_by_rule = {}
    order_by_rule = {}
    rule_by_order = {}
    for record in records:
        if (not isinstance(record, dict) or record.get("appId") != app_id
                or record.get("status") != "VERIFIED" or not isinstance(record.get("recordId"), str)
                or not record.get("recordId")):
            raise ValueError("Acceptance table contains an invalid or unverified record")
        if record["recordId"] in seen_ids:
            raise ValueError("Acceptance table contains duplicate recordId values")
        seen_ids.add(record["recordId"])
        identity = record.get("identity", {})
        if (not isinstance(identity, dict) or identity.get("packageName") != policy.package_name
                or identity.get("abi") not in policy.supported_abis
                or identity.get("signerSha256", "").lower() != policy.signer_sha256
                or type(identity.get("versionCode")) is not int or identity["versionCode"] <= 0
                or not _DIGEST.fullmatch(identity.get("apkSha256", ""))):
            raise ValueError("Acceptance record APK identity does not match app policy")
        plan = record.get("expectedPlan")
        rule_id = plan.get("ruleId") if isinstance(plan, dict) else None
        if isinstance(plan, dict) and isinstance(plan.get('targets'), dict):
            for target_id, target in plan['targets'].items():
                _validate_target(target_id, target)
        source = record.get("source", {})
        try:
            if not isinstance(source, dict) or source.get('type') not in {'official', 'signed-download'}:
                raise ValueError('Acceptance source type is invalid')
            if source['type'] == 'official':
                validate_official_apk_url(source.get('url', ''), policy.official_apk_prefix)
            else:
                validate_download_url(source.get('url', ''))
            if source.get('archiveSha256') is not None and not _DIGEST.fullmatch(str(source['archiveSha256'])):
                raise ValueError('Acceptance source archive hash is invalid')
            if source['type'] == 'signed-download' and not source.get('archiveSha256'):
                raise ValueError('Signed download acceptance requires a pinned archive hash')
        except ValueError as error:
            raise ValueError("Acceptance record official APK source is invalid") from error
        apk_files = source.get("apkFiles", [])
        if (not isinstance(apk_files, list) or not apk_files or not isinstance(apk_files[0], dict)
                or apk_files[0].get("role") != "base"
                or apk_files[0].get("sha256", "").lower() != identity["apkSha256"].lower()
                or any(not isinstance(item, dict)
                       or item.get("role") != ("base" if index == 0 else "split")
                       or not isinstance(item.get("path"), str) or not item['path']
                       or Path(item['path']).is_absolute() or '..' in Path(item['path']).parts
                       or not isinstance(item.get("sha256"), str)
                       or not _DIGEST.fullmatch(item.get("sha256", ""))
                       for index, item in enumerate(apk_files))):
            raise ValueError("Acceptance record base/split identities are incomplete")
        order = record.get("ruleOrder")
        first_id = record.get('firstRecordId')
        if (not isinstance(rule_id, str) or type(order) is not int or order < 1
                or type(record.get('firstForRule')) is not bool or not isinstance(first_id, str)):
            raise ValueError("Acceptance record is missing its rule identity or order")
        if rule_id in order_by_rule and order_by_rule[rule_id] != order:
            raise ValueError('Acceptance table has inconsistent rule order')
        if order in rule_by_order and rule_by_order[order] != rule_id:
            raise ValueError('Acceptance table reuses a rule order')
        order_by_rule[rule_id] = order
        rule_by_order[order] = rule_id
        if record.get("firstForRule"):
            if rule_id in first_by_rule:
                raise ValueError("Acceptance table has more than one first record for a rule")
            first_by_rule[rule_id] = record
        if not record.get('firstRecordId'):
            raise ValueError('Acceptance table fixed first record ID is missing')
    if records:
        catalog = catalog or load_rule_catalog(app_id, repository)
        if catalog.get('appId') != app_id:
            raise ValueError('Acceptance rule catalog belongs to another application')
        for rule_id, group in ((key, [row for row in records if row['expectedPlan']['ruleId'] == key])
                               for key in order_by_rule):
            if sorted(order_by_rule.values()) != list(range(1, len(order_by_rule) + 1)):
                raise ValueError('Acceptance table rule order is not a contiguous creation sequence')
            first = group[0]
            if (first.get('firstForRule') is not True or first.get('firstRecordId') != first.get('recordId')
                    or first_by_rule.get(rule_id, {}).get('recordId') != first.get('recordId')
                    or any(row.get('firstForRule') is not False
                           or row.get('firstRecordId') != first.get('recordId')
                           for row in group[1:])):
                raise ValueError(f'Acceptance fixed first record is invalid for {rule_id}')
            for record in group:
                route = record.get('route')
                module = record.get('module')
                if (not isinstance(route, dict) or route.get('appId') != app_id
                        or not isinstance(route.get('candidates'), list)
                        or record['expectedPlan']['ruleId'] not in route['candidates']
                        or not isinstance(module, dict)):
                    raise ValueError('Acceptance record route or module identity is incomplete')
                if (not _DIGEST.fullmatch(record.get('ruleCatalogSha256', ''))
                        or record.get('ruleCatalogSha256') != module.get('ruleCatalogSha256')):
                    raise ValueError('Acceptance record catalog digest is not bound to its tested module')
                validate_expected_plan(record['expectedPlan'], catalog,
                                       record['identity']['versionCode'])
                route_payload = {'appId': app_id, 'candidates': route['candidates']}
                route_sha = hashlib.sha256(json.dumps(
                    route_payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
                if (route_sha != module.get('routeSha256')
                        or module.get('route', {}).get('appId') != app_id
                        or module.get('route', {}).get('candidates') != route['candidates']):
                    raise ValueError('Acceptance route fingerprint does not match the tested module')
                module_digests = ('sha256', 'sourceTreeSha256', 'ruleCatalogSha256', 'routeSha256',
                                  'routeConfigurationSha256', 'dependencyLockSha256',
                                  'fingerprintFileSha256')
                if any(not _DIGEST.fullmatch(module.get(key, '')) for key in module_digests):
                    raise ValueError('Acceptance record module source fingerprint is incomplete')
                fingerprint = module.get('fingerprint')
                if (not isinstance(fingerprint, dict) or fingerprint.get('schemaVersion') != 1
                        or fingerprint.get('sourceTreeSha256') != module.get('sourceTreeSha256')
                        or fingerprint.get('routeConfigurationSha256') != module.get('routeConfigurationSha256')
                        or fingerprint.get('dependencyLockSha256') != module.get('dependencyLockSha256')):
                    raise ValueError('Acceptance record APK fingerprint is incomplete or inconsistent')
                stage_evidence = record.get('stageEvidence')
                if (not isinstance(stage_evidence, dict)
                        or set(stage_evidence) != {'static', 'regression', 'runtime'}
                        or any(not isinstance(ref, dict) or not ref.get('path')
                               or not _DIGEST.fullmatch(ref.get('sha256', ''))
                               for ref in stage_evidence.values())):
                    raise ValueError('Acceptance record stage evidence references are incomplete')
    return records


def select_representatives(records: list[dict]) -> list[dict]:
    if not records:
        return []
    app_ids = {record.get("appId") for record in records}
    if len(app_ids) != 1:
        raise ValueError("Representative selection requires one explicit appId")
    rule_orders = {}
    first_by_rule = {}
    groups = {}
    order_to_rule = {}
    for record in records:
        rule_id = record.get("expectedPlan", {}).get("ruleId")
        order = record.get("ruleOrder")
        if not isinstance(rule_id, str) or type(order) is not int or order < 1:
            raise ValueError("Acceptance record is missing its rule identity or order")
        if rule_id in rule_orders and rule_orders[rule_id] != order:
            raise ValueError("A rule has inconsistent creation order")
        if order in order_to_rule and order_to_rule[order] != rule_id:
            raise ValueError('Different rules share an acceptance order')
        rule_orders[rule_id] = order
        order_to_rule[order] = rule_id
        groups.setdefault(rule_id, []).append(record)
        if record.get("firstForRule") is True:
            if rule_id in first_by_rule:
                raise ValueError("A rule has more than one fixed first record")
            first_by_rule[rule_id] = record
        first_id = record.get('firstRecordId')
        if not isinstance(first_id, str) or not first_id:
            raise ValueError('Acceptance record has no fixed first record ID')
    if sorted(rule_orders.values()) != list(range(1, len(rule_orders) + 1)):
        raise ValueError('Acceptance rule order is not a contiguous validated snapshot')
    for rule_id, group in groups.items():
        first = group[0]
        if (first.get('firstForRule') is not True or first.get('firstRecordId') != first.get('recordId')
                or first_by_rule.get(rule_id, {}).get('recordId') != first.get('recordId')
                or any(row.get('firstForRule') is not False
                       or row.get('firstRecordId') != first.get('recordId') for row in group[1:])):
            raise ValueError(f'Fixed first record is invalid for {rule_id}')
    latest_rules = sorted(rule_orders, key=lambda rule: (rule_orders[rule], rule))[-3:]
    selected = []
    seen_sha = set()
    for rule_id in latest_rules:
        representative = first_by_rule.get(rule_id)
        if representative is None:
            raise ValueError(f"The fixed first acceptance record for {rule_id} is missing")
        digest = representative.get("identity", {}).get("apkSha256", "").lower()
        if not _DIGEST.fullmatch(digest):
            raise ValueError(f"The fixed representative for {rule_id} has no valid APK SHA-256")
        if digest not in seen_sha:
            selected.append(representative)
            seen_sha.add(digest)
    return selected


def make_record(candidate: dict, module: dict, validation: dict, *, candidate_path: Path | None = None,
                module_apk: Path | None = None, regression_path: Path | None = None,
                smoke_path: Path | None = None, evidence_root: Path | None = None,
                acceptance_path: Path | None = None, initial_migration: bool = False,
                repository: Path | None = None, conclusion: str = 'success') -> dict:
    if (not isinstance(validation, dict)
            or any(value is None for value in (candidate_path, module_apk, regression_path, smoke_path,
                                               evidence_root, acceptance_path))):
        raise ValueError('Registration requires candidate, module, regression, runtime, and acceptance files')
    from scripts.ci.runtime_task import verify_validation_files
    snapshot_object = validation.get('_acceptanceSnapshotObject')
    if validation.get('acceptanceSnapshot') is not None and snapshot_object is None:
        raise ValueError('Registration requires the verified acceptance snapshot object')
    rebuilt = verify_validation_files(candidate_path, module_apk, regression_path, smoke_path,
                                      evidence_root, conclusion, repository or Path.cwd(),
                                      acceptance_path, initial_migration,
                                      acceptance_snapshot=snapshot_object)
    if (validation.get('stageEvidence') != rebuilt.get('stageEvidence')
            or validation.get('acceptanceSnapshot') != rebuilt.get('acceptanceSnapshot')):
        raise ValueError('Stage evidence or acceptance snapshot changed after unified validation')
    if rebuilt.get('outcome') != 'VERIFIED':
        raise ValueError('Unified file-backed validation did not produce VERIFIED')
    candidate = rebuilt['candidate']
    module = rebuilt['module']
    identity = candidate['identity']
    plan = candidate['expectedPlan']
    snapshot_binding = rebuilt.get('acceptanceSnapshot')
    if (snapshot_object is None
            or rebuilt.get('_acceptanceSnapshotObject') is not snapshot_object
            or validation.get('acceptanceSnapshot') != snapshot_binding):
        raise ValueError('Acceptance snapshot changed after unified validation')
    snapshot = validate_acceptance_snapshot(
        snapshot_object, candidate['appId'], initial_migration,
        catalog=rebuilt['static'].get('ruleCatalog'), repository=repository)
    record_id = str(uuid.uuid4())
    order = derive_record_order(snapshot, plan['ruleId'], record_id, identity['apkSha256'])
    runtime = rebuilt['runtime']
    evidence = runtime['evidence']
    try:
        validate_official_apk_url(candidate['sourceUrl'], get_policy(candidate['appId']).official_apk_prefix)
        source_type = 'official'
    except ValueError:
        validate_download_url(candidate['sourceUrl'])
        source_type = 'signed-download'
    source = {'type': source_type, 'url': candidate['sourceUrl'], 'apkFiles': candidate['apkFiles']}
    if candidate.get('sourceArchiveSha256'):
        source['archiveSha256'] = candidate['sourceArchiveSha256']
    elif source_type == 'signed-download':
        raise ValueError('Signed download acceptance requires a pinned archive hash')
    record = {
        "recordId": record_id,
        "schemaVersion": SCHEMA_VERSION,
        "appId": candidate["appId"],
        "status": "VERIFIED",
        "identity": identity,
        "source": source,
        "expectedPlan": plan,
        "route": candidate.get("route", {}),
        "module": module,
        "ruleCatalogSha256": module['ruleCatalogSha256'],
        "contract": runtime.get("requestIdentityExpectation", {}).get("contract") or runtime.get("contract"),
        "evidence": evidence,
        "stageEvidence": rebuilt['stageEvidence'],
        "acceptedAt": datetime.now(timezone.utc).isoformat(),
        **order,
        "supersedes": candidate.get("supersedes"),
    }
    return record
