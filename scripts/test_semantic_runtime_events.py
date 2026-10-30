import base64
import copy
import json
import unittest
from pathlib import Path

from scripts.ci.semantic_protocol import plan_execution_sha256
from scripts.ci.semantic_runtime_events import validate_semantic_install_events


ROOT = Path(__file__).resolve().parents[1]
PROFILES = json.loads((ROOT / 'compatibility/semantic-migration/expectations.json').read_text())['profiles']
PROCESS = 'com.tencent.example'


def event_pair(profile, *, plan=None, plan_hash=None, plan_time=100, hook_time=200,
               process=PROCESS, protocol=2, attempts=1, cache_hit=False,
               hook_changes=None, plan_suffix=''):
    plan = copy.deepcopy(profile['expectedPlan'] if plan is None else plan)
    catalog, snapshot = profile['ruleCatalog'], profile['snapshot']
    execution_hash = plan_execution_sha256(plan) if plan_hash is None else plan_hash
    encoded = base64.urlsafe_b64encode(json.dumps(plan, separators=(',', ':')).encode()).decode().rstrip('=')
    plan_body = (
        'rule-runtime event=semantic_plan '
        f'protocolVersion={protocol} elapsedRealtimeNs={plan_time} executionSha256={execution_hash} '
        f'plan={encoded} cacheHit={str(cache_hit).lower()} attempts={attempts}{plan_suffix}'
    )
    hook_fields = {
        'protocolVersion': protocol,
        'elapsedRealtimeNs': hook_time,
        'contractId': plan['contractId'],
        'contractRevision': plan['contractRevision'],
        'strategyId': plan['strategyId'],
        'strategyRevision': plan['strategyRevision'],
        'executionSha256': execution_hash,
        'snapshotSha256': plan['snapshotSha256'],
        'hookCount': len(plan['hooks']),
        'processName': process,
    }
    hook_fields.update(hook_changes or {})
    hook_body = 'rule-runtime event=hook_installed ' + ' '.join(f'{key}={value}' for key, value in hook_fields.items())
    return plan_body, hook_body, plan, catalog, snapshot


class SemanticRuntimeEventsTest(unittest.TestCase):
    def test_decodes_valid_complete_plan_events_for_all_three_profiles(self):
        for profile in PROFILES:
            with self.subTest(profile=profile['profileId']):
                plan_body, hook_body, plan, catalog, snapshot = event_pair(profile)
                proof = validate_semantic_install_events(
                    plan_body, hook_body, plan, catalog, snapshot, PROCESS,
                )
                self.assertEqual(2, proof['protocolVersion'])
                self.assertEqual(plan, proof['completePlan'])
                self.assertEqual(plan_execution_sha256(plan), proof['executionSha256'])
                self.assertEqual(snapshot['contentSha256'], proof['snapshotSha256'])
                self.assertEqual(len(plan['hooks']), proof['hookCount'])
                self.assertEqual(PROCESS, proof['processName'])
                self.assertFalse(proof['cacheHit'])
                self.assertEqual(1, proof['attempts'])
                self.assertEqual(100, proof['planElapsedRealtimeNs'])
                self.assertEqual(200, proof['hookElapsedRealtimeNs'])

    def test_accepts_cache_hit_only_on_first_attempt_and_second_attempt_miss(self):
        profile = next(row for row in PROFILES if row['appId'] == 'qq')
        for cache_hit, attempts in ((True, 1), (False, 2)):
            with self.subTest(cache_hit=cache_hit, attempts=attempts):
                values = event_pair(profile, cache_hit=cache_hit, attempts=attempts)
                proof = validate_semantic_install_events(*values[:2], *values[2:], PROCESS)
                self.assertEqual(cache_hit, proof['cacheHit'])
                self.assertEqual(attempts, proof['attempts'])

    def test_rejects_malformed_truncated_and_noncanonical_base64(self):
        profile = PROFILES[0]
        plan_body, hook_body, plan, catalog, snapshot = event_pair(profile)
        for malformed in ('', plan_body[:-20], plan_body.replace('plan=', 'plan=%%%')):
            with self.subTest(body=malformed[:70]), self.assertRaises(ValueError):
                validate_semantic_install_events(malformed, hook_body, plan, catalog, snapshot, PROCESS)

    def test_rejects_duplicate_tokens_and_duplicate_json_fields(self):
        profile = PROFILES[0]
        plan_body, hook_body, plan, catalog, snapshot = event_pair(profile)
        duplicate_token = plan_body + ' attempts=1'
        with self.assertRaises(ValueError):
            validate_semantic_install_events(duplicate_token, hook_body, plan, catalog, snapshot, PROCESS)

        encoded_plan = base64.urlsafe_b64encode(
            ('{"schemaVersion":1,' + json.dumps(plan, separators=(',', ':'))[1:]).encode(),
        ).decode().rstrip('=')
        duplicate_json_body = plan_body[:plan_body.index('plan=')] + f'plan={encoded_plan}' + plan_body[plan_body.index(' cacheHit='):]
        with self.assertRaises(ValueError):
            validate_semantic_install_events(duplicate_json_body, hook_body, plan, catalog, snapshot, PROCESS)

    def test_rejects_plan_digest_mismatch_and_changed_execution_fields(self):
        profile = next(row for row in PROFILES if row['appId'] == 'qq')
        original = profile['expectedPlan']
        changes = (
            lambda value: value['hooks'][0].update(descriptor='Lfixture/Changed;->x()V'),
            lambda value: value['hooks'][0]['source'].update(dexSha256='f' * 64),
            lambda value: value['hooks'][0].update(configuration={'enum': 'PHONE'}),
            lambda value: value['hooks'][0]['shape'].update(returnType='int'),
        )
        for change in changes:
            plan = copy.deepcopy(original)
            change(plan)
            values = event_pair(profile, plan=plan, plan_hash=plan_execution_sha256(original))
            with self.assertRaises(ValueError):
                validate_semantic_install_events(*values[:2], original, profile['ruleCatalog'], profile['snapshot'], PROCESS)

        values = event_pair(profile, plan_hash='0' * 64)
        with self.assertRaises(ValueError):
            validate_semantic_install_events(*values[:2], original, profile['ruleCatalog'], profile['snapshot'], PROCESS)

    def test_rejects_hook_contract_source_process_timestamp_and_protocol_mismatch(self):
        profile = PROFILES[0]
        plan_body, hook_body, plan, catalog, snapshot = event_pair(profile)
        for field, value in (
            ('contractId', 'other'),
            ('contractRevision', 9),
            ('strategyId', 'other'),
            ('strategyRevision', 9),
            ('executionSha256', '0' * 64),
            ('snapshotSha256', '0' * 64),
            ('hookCount', 999),
            ('processName', 'com.tencent.other'),
        ):
            changed = 'rule-runtime event=hook_installed ' + ' '.join(
                f'{key}={value if key == field else token.split("=", 1)[1]}'
                for token in hook_body.split()[2:]
                for key in (token.split('=', 1)[0],)
            )
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_semantic_install_events(plan_body, changed, plan, catalog, snapshot, PROCESS)

        for kwargs in (
            {'process': 'com.tencent.other'},
            {'plan_time': 200, 'hook_time': 200},
            {'plan_time': 201, 'hook_time': 200},
            {'plan_time': 0},
            {'protocol': 1},
            {'attempts': 0},
            {'attempts': 3},
            {'cache_hit': True, 'attempts': 2},
        ):
            values = event_pair(profile, **kwargs)
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                validate_semantic_install_events(*values[:2], plan, catalog, snapshot, PROCESS)


if __name__ == '__main__':
    unittest.main()
