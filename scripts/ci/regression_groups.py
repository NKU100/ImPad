"""Pure deterministic grouping and bounded selection for accepted v2 cases."""
import hashlib
from datetime import datetime, timezone
import json
import re

GROUPING_VERSION = 1
MAX_GROUPS = 3
_FIELD = re.compile(r'^L([^;]+);->[^:]+:(.+)$')
_METHOD = re.compile(r'^L([^;]+);->[^ (]+\(')


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def _digest(value):
    return hashlib.sha256(_json(value).encode('utf-8')).hexdigest()


def _apk_identity(case):
    return tuple((row.get('role'), row.get('sha256', '').lower()) for row in case['apkFiles'])


def _recognizer_winners(selection):
    evidence = selection.get('evidenceByRecognizer')
    if not isinstance(evidence, dict) or not evidence:
        return None
    return sorted(evidence)


def _type_shape(value):
    if not isinstance(value, str):
        return None
    if value.endswith('[]'):
        return {'arrayOf': _type_shape(value[:-2])}
    if value in {'boolean', 'byte', 'char', 'short', 'int', 'long', 'float', 'double', 'void'}:
        return value
    if value.startswith(('android.', 'java.', 'javax.')):
        return value
    return 'app_reference'


def _dex_type_shape(value):
    if not isinstance(value, str) or not value:
        return None
    primitive = {'Z': 'boolean', 'B': 'byte', 'C': 'char', 'S': 'short', 'I': 'int',
                 'J': 'long', 'F': 'float', 'D': 'double', 'V': 'void'}
    if value.startswith('['):
        component = _dex_type_shape(value[1:])
        return {'arrayOf': component} if component is not None else None
    if value in primitive:
        return primitive[value]
    if value.startswith('L') and value.endswith(';'):
        return _type_shape(value[1:-1].replace('/', '.'))
    return None


def _shape(shape):
    if not isinstance(shape, dict) or not isinstance(shape.get('parameters'), list):
        return None
    parameters = [_type_shape(value) for value in shape['parameters']]
    result = _type_shape(shape.get('returnType'))
    if result is None or any(value is None for value in parameters) or not isinstance(shape.get('isStatic'), bool):
        return None
    return {'parameters': parameters, 'returnType': result, 'isStatic': shape['isStatic']}


def _class_relation(owner, hook):
    method = _METHOD.match(hook.get('descriptor', ''))
    if not method:
        return None
    return 'HOOK_CLASS' if owner == method.group(1) else 'OTHER_CLASS'


def _field_binding(value, hook, *, monitor=False):
    match = _FIELD.fullmatch(value) if isinstance(value, str) else None
    if not match:
        return None
    relation = _class_relation(match.group(1), hook)
    if relation is None:
        return None
    kind = ('SAME_CLASS_FIELD' if relation == 'HOOK_CLASS' else 'OTHER_CLASS_FIELD') if monitor else relation
    field_type = _dex_type_shape(match.group(2))
    if field_type is None:
        return None
    return {'kind': kind, 'fieldType': field_type}


def _monitor_binding(value, hook):
    if isinstance(value, str) and value.startswith('L') and value.endswith(';') and '->' not in value:
        relation = _class_relation(value[1:-1], hook)
        if relation is None:
            return None
        return {'kind': 'DECLARATION_CLASS' if relation == 'HOOK_CLASS' else 'OTHER_CLASS_DECLARATION'}
    return _field_binding(value, hook, monitor=True)


def _behavior(case):
    report = case.get('savedStaticReport')
    selection = report.get('selection') if isinstance(report, dict) else None
    plan = selection.get('plan') if isinstance(selection, dict) else None
    if not isinstance(plan, dict):
        return None
    winners = _recognizer_winners(selection)
    hooks = plan.get('hooks')
    if winners is None or not isinstance(hooks, list) or not hooks:
        return None
    normalized_hooks = []
    for hook in hooks:
        if not isinstance(hook, dict) or not all(k in hook for k in ('role', 'phase', 'shape', 'configuration')):
            return None
        config = hook['configuration']
        if not isinstance(config, dict):
            return None
        normalized_config = {}
        for key, value in sorted(config.items()):
            if key == 'monitorField':
                binding = _monitor_binding(value, hook)
                if binding is None:
                    return None
                normalized_config[key] = binding
            elif key == 'cacheField':
                binding = _field_binding(value, hook)
                if binding is None:
                    return None
                normalized_config[key] = binding
            else:
                normalized_config[key] = value
        shape = _shape(hook['shape'])
        if shape is None:
            return None
        normalized_hooks.append({'role': hook['role'], 'phase': hook['phase'],
                                 'shape': shape, 'configuration': normalized_config})
    normalized_hooks.sort(key=_json)
    required = ('contractId', 'contractRevision', 'strategyId', 'strategyRevision')
    if any(key not in plan for key in required):
        return None
    result = {'recognizerWinners': winners,
              **{key: plan[key] for key in required}, 'hooks': normalized_hooks}
    if case.get('appId') == 'wechat':
        features = selection.get('behaviorFeatures')
        mode = features.get('wrapperMode') if isinstance(features, dict) else None
        if not isinstance(mode, str) or mode not in {'inline', 'aggregate'}:
            return None
        result['behaviorFeatures'] = {'wrapperMode': features['wrapperMode']}
    return result


def _case_order(case):
    identity = case.get('identity') or {}
    try:
        version = int(identity.get('versionCode', -1))
    except (TypeError, ValueError):
        version = -1
    return (-version, -_accepted_at(case).timestamp(), str(case.get('recordId', '')))


def _accepted_at(case):
    try:
        accepted = datetime.fromisoformat(str(case.get('acceptedAt', '')).replace('Z', '+00:00'))
    except (ValueError, OverflowError):
        return datetime.min.replace(tzinfo=timezone.utc)
    if accepted.tzinfo is None:
        accepted = accepted.replace(tzinfo=timezone.utc)
    return accepted.astimezone(timezone.utc)


def _duplicate_rank(case, behavior):
    return behavior is not None, _accepted_at(case), str(case.get('recordId', ''))


def select_regression_groups(cases):
    """Group accepted-v2 cases by structural behavior and choose at most three.

    Cases with incomplete structural evidence remain isolated by complete APK
    identity; diagnostic prose is deliberately never parsed.
    """
    buckets = {}
    unique_inputs = {}
    app_ids = set()
    for case in cases:
        if case.get('kind') != 'ACCEPTED_V2':
            raise ValueError('Regression grouping accepts ACCEPTED_V2 cases only')
        app_ids.add(case.get('appId'))
        if len(app_ids) > 1:
            raise ValueError('Regression grouping accepts one appId at a time')
        apk_id = (case.get('appId'), _apk_identity(case))
        behavior = _behavior(case)
        prior = unique_inputs.get(apk_id)
        if prior is not None:
            previous_case, previous_behavior = prior
            if _duplicate_rank(case, behavior) > _duplicate_rank(previous_case, previous_behavior):
                unique_inputs[apk_id] = (case, behavior)
            continue
        unique_inputs[apk_id] = (case, behavior)
    for case, behavior in unique_inputs.values():
        key = {'appId': case.get('appId'), 'behavior': behavior} if behavior is not None else {
            'unknownInput': {'appId': case.get('appId'), 'apkFiles': _apk_identity(case)}}
        buckets.setdefault(_json(key), {'key': key, 'behavior': behavior, 'cases': []})['cases'].append(case)

    groups = []
    for encoded, bucket in buckets.items():
        ordered = sorted(bucket['cases'], key=_case_order)
        groups.append({'groupId': 'rg1-' + _digest(bucket['key'])[:16],
                       'cases': ordered, 'preferredCase': ordered[0],
                       'evidenceComplete': bucket['behavior'] is not None})
    groups.sort(key=lambda group: (_case_order(group['preferredCase']), group['groupId']))
    selected = groups[:MAX_GROUPS]
    return {'version': GROUPING_VERSION, 'groups': groups,
            'selectedGroups': selected, 'excludedGroups': groups[MAX_GROUPS:]}
