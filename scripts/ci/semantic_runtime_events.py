"""Validate protocol-v2 semantic event pairs only.

Callers must separately verify the scoped outer PID envelope, runtime readiness,
and native identities. This proof does not claim ART method-origin provenance.
"""

import base64
import binascii
import json
import re

from scripts.ci.semantic_protocol import plan_execution_sha256, validate_complete_plan


_SHA256 = re.compile(r'[0-9a-f]{64}')
_URLSAFE_BASE64 = re.compile(r'[A-Za-z0-9_-]+')
_PLAN_FIELDS = {
    'protocolVersion', 'elapsedRealtimeNs', 'executionSha256', 'plan', 'cacheHit', 'attempts',
}
_HOOK_FIELDS = {
    'protocolVersion', 'elapsedRealtimeNs', 'contractId', 'contractRevision', 'strategyId',
    'strategyRevision', 'executionSha256', 'snapshotSha256', 'hookCount', 'processName',
}


def validate_semantic_install_events(plan_body, hook_body, expected_plan, catalog, snapshot, expected_process):
    """Return proof of the expected plan/event pair, not process or native identity."""
    plan_fields = _parse_event(plan_body, 'semantic_plan', _PLAN_FIELDS)
    hook_fields = _parse_event(hook_body, 'hook_installed', _HOOK_FIELDS)
    if plan_fields['protocolVersion'] != '2' or hook_fields['protocolVersion'] != '2':
        raise ValueError('Unsupported semantic runtime event protocol')

    plan_elapsed = _positive_integer(plan_fields['elapsedRealtimeNs'], 'plan timestamp')
    hook_elapsed = _positive_integer(hook_fields['elapsedRealtimeNs'], 'hook timestamp')
    if hook_elapsed <= plan_elapsed:
        raise ValueError('Hook installation must follow semantic plan resolution')

    if plan_fields['attempts'] not in {'1', '2'}:
        raise ValueError('Invalid semantic resolution attempt count')
    attempts = int(plan_fields['attempts'])
    if plan_fields['cacheHit'] not in {'true', 'false'}:
        raise ValueError('Invalid semantic cache-hit value')
    cache_hit = plan_fields['cacheHit'] == 'true'
    if cache_hit and attempts != 1:
        raise ValueError('A cache hit must complete on the first attempt')

    encoded = plan_fields['plan']
    if len(encoded) > 2800 or not _URLSAFE_BASE64.fullmatch(encoded):
        raise ValueError('Invalid encoded complete plan')
    try:
        raw_plan = base64.b64decode(encoded + '=' * (-len(encoded) % 4), altchars=b'-_', validate=True)
        if base64.urlsafe_b64encode(raw_plan).decode('ascii').rstrip('=') != encoded:
            raise ValueError('Noncanonical plan Base64')
        decoded_plan = json.loads(raw_plan.decode('utf-8'), object_pairs_hook=_unique_json_object)
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError('Malformed encoded complete plan') from error
    if not isinstance(decoded_plan, dict):
        raise ValueError('Complete plan must be a JSON object')

    expected_execution = validate_complete_plan(expected_plan, catalog, snapshot)
    decoded_execution = validate_complete_plan(decoded_plan, catalog, snapshot)
    if decoded_execution != expected_execution or decoded_execution != plan_execution_sha256(expected_plan):
        raise ValueError('Runtime plan differs from the expected complete plan')
    if plan_fields['executionSha256'] != decoded_execution:
        raise ValueError('Semantic plan event execution identity differs')

    expected_hook_values = {
        'contractId': decoded_plan['contractId'],
        'contractRevision': str(decoded_plan['contractRevision']),
        'strategyId': decoded_plan['strategyId'],
        'strategyRevision': str(decoded_plan['strategyRevision']),
        'executionSha256': decoded_execution,
        'snapshotSha256': decoded_plan['snapshotSha256'],
        'hookCount': str(len(decoded_plan['hooks'])),
        'processName': expected_process,
    }
    if not isinstance(expected_process, str) or not expected_process or any(char.isspace() for char in expected_process):
        raise ValueError('Invalid expected process name')
    for field, expected in expected_hook_values.items():
        if hook_fields[field] != expected:
            raise ValueError(f'Hook installation event {field} differs from the validated plan')

    return {
        'protocolVersion': 2,
        'completePlan': decoded_plan,
        'executionSha256': decoded_execution,
        'snapshotSha256': decoded_plan['snapshotSha256'],
        'hookCount': len(decoded_plan['hooks']),
        'processName': expected_process,
        'cacheHit': cache_hit,
        'attempts': attempts,
        'planElapsedRealtimeNs': plan_elapsed,
        'hookElapsedRealtimeNs': hook_elapsed,
    }


def _parse_event(body, expected_event, expected_fields):
    if not isinstance(body, str):
        raise ValueError('Runtime event body must be text')
    tokens = body.split()
    if len(tokens) < 2 or tokens[:2] != ['rule-runtime', f'event={expected_event}']:
        raise ValueError(f'Expected semantic runtime event {expected_event}')
    fields = {}
    for token in tokens[2:]:
        key, separator, value = token.partition('=')
        if not separator or not key or not value or key in fields:
            raise ValueError('Malformed or duplicate runtime event field')
        fields[key] = value
    if set(fields) != expected_fields:
        raise ValueError(f'Runtime event {expected_event} has an invalid field set')
    return fields


def _unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f'Duplicate JSON field: {key}')
        result[key] = value
    return result


def _positive_integer(value, label):
    if not re.fullmatch(r'[1-9][0-9]*', value):
        raise ValueError(f'Invalid {label}')
    return int(value)
