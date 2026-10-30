import base64
import copy
import json
import unittest

from scripts.ci.semantic_protocol import catalog_sha256, plan_execution_sha256, snapshot_sha256
from scripts.ci.semantic_hook_evidence import derive_semantic_hook_evidence


MODULE = 'io.github.nku100.impad'
API_SHA = 'c' * 64
NATIVE_SHA = 'd' * 64
DEX_SHA = 'a' * 64
CONTAINER_SHA = 'b' * 64
PACKAGES = {'wechat': 'com.tencent.mm', 'wecom': 'com.tencent.wework', 'qq': 'com.tencent.mobileqq'}


def protocol_two_logcat(candidate, module, *, process_name=None, native_path='/data/app/impad/base.apk', pid=3100):
    app_id = candidate['appId']
    package = candidate['identity']['packageName']
    process_name = process_name or (f'{package}:MSF' if app_id == 'qq' else package)
    plan = candidate['expectedPlan']
    snapshot = candidate['snapshot']
    execution_sha = candidate['expectedExecutionSha256']
    dexkit = module['dexkit']
    encoded_plan = base64.urlsafe_b64encode(json.dumps(plan, separators=(',', ':')).encode()).decode().rstrip('=')
    ready_event = 'qfix_ready' if app_id == 'qq' else 'tinker_ready'
    ready_body = (f'rule-runtime event={ready_event} elapsedRealtimeNs=100 contextPackage={package} '
                  'loaderClass=dalvik.system.PathClassLoader')
    native_body = (f'rule-runtime event=native_loaded elapsedRealtimeNs=200 apiSha256={dexkit["dexkitAndroidApiSha256"]} '
                   f'nativeSha256={dexkit["dexkitAndroidArm64NativeSha256"]} nativeAbi={candidate["identity"]["abi"]} '
                   f'packagingMode=APK_DIRECT sourceApkPath={native_path} '
                   f'nativeLoadPath={native_path}!/lib/{candidate["identity"]["abi"]}/libdexkit.so')
    plan_body = (f'rule-runtime event=semantic_plan protocolVersion=2 elapsedRealtimeNs=250 '
                 f'executionSha256={execution_sha} plan={encoded_plan} cacheHit=false attempts=1')
    hook_body = (f'rule-runtime event=hook_installed protocolVersion=2 elapsedRealtimeNs=300 '
                 f"contractId={plan['contractId']} contractRevision={plan['contractRevision']} "
                 f"strategyId={plan['strategyId']} strategyRevision={plan['strategyRevision']} "
                 f'executionSha256={execution_sha} snapshotSha256={snapshot["contentSha256"]} '
                 f'hookCount={len(plan["hooks"])} processName={process_name}')
    bodies = [ready_body, native_body, plan_body, hook_body]
    return '\n'.join(
        f'10-21 00:00:0{index}.000 {pid} {pid} I ImPad: ({process_name})[{MODULE},ImPad,1.0.0,1,1] {body}'
        for index, body in enumerate(bodies, 1)) + '\n'


def lifecycle_fixture(app_id='wechat', *, apk_sha=CONTAINER_SHA, api_sha=API_SHA,
                      native_sha=NATIVE_SHA, native_path='/data/app/impad/base.apk', version_code=10):
    package = PACKAGES[app_id]
    roles = {
        'wechat': ['tablet', 'login'],
        'wecom': ['app_pad', 'device_type', 'service_pad'],
        'qq': ['device_type'],
    }[app_id]
    catalog = {
        'schemaVersion': 2, 'appId': app_id, 'ruleCatalogSha256': '',
        'contractId': f'{app_id}_contract', 'contractRevision': 3,
        'requiredRoles': roles, 'recognizerIds': [f'{app_id}_recognizer'],
        'strategies': [{'id': f'{app_id}_strategy', 'revision': 4,
                       'roles': [{'role': role, 'phase': 'BEFORE', 'configuration': {'mode': 'tablet'}}
                                 for role in roles]}],
        'versionHints': [],
    }
    catalog['ruleCatalogSha256'] = catalog_sha256(catalog)
    snapshot = {
        'protocolVersion': 1, 'readyLoaderId': 'loader-0', 'contentSha256': '',
        'loaders': [{'id': 'loader-0', 'parentId': None, 'delegation': 'PARENT_FIRST', 'dexSha256': [DEX_SHA]}],
        'archives': [{'loaderId': 'loader-0', 'containerIndex': 0, 'kind': 'ZIP',
                      'containerSha256': apk_sha, 'dexSha256': [DEX_SHA]}],
    }
    snapshot['contentSha256'] = snapshot_sha256(snapshot)
    plan = {
        'schemaVersion': 1, 'snapshotSha256': snapshot['contentSha256'],
        'contractId': catalog['contractId'], 'contractRevision': catalog['contractRevision'],
        'strategyId': catalog['strategies'][0]['id'], 'strategyRevision': 4,
        'hooks': [{'role': role, 'descriptor': f'Lfixture/Entry;->run{index}()V',
                   'shape': {'parameters': [], 'returnType': 'void', 'isStatic': True},
                   'source': {'loaderId': 'loader-0', 'dexIndex': 0, 'dexSha256': DEX_SHA},
                   'phase': 'BEFORE', 'configuration': {'mode': 'tablet'}}
                  for index, role in enumerate(roles)],
    }
    execution_sha = plan_execution_sha256(plan)
    candidate = {
        'schemaVersion': 2, 'appId': app_id, 'status': 'PREPARED',
        'identity': {'packageName': package, 'versionCode': version_code, 'abi': 'arm64-v8a', 'apkSha256': apk_sha},
        'apkFiles': [{'role': 'base', 'path': 'base.apk', 'sha256': apk_sha}],
        'ruleCatalog': catalog, 'ruleCatalogSha256': catalog['ruleCatalogSha256'], 'snapshot': snapshot,
        'expectedPlan': plan, 'expectedExecutionSha256': execution_sha,
    }
    module = {'dexkit': {'dexkitAndroidApiSha256': api_sha, 'dexkitAndroidArm64NativeSha256': native_sha}}
    process = package + ':MSF' if app_id == 'qq' else package
    process_text = f'3100 {process}\n'
    native_path = native_path
    logs = protocol_two_logcat(candidate, module, process_name=process, native_path=native_path)
    return candidate, module, process_text, logs, {'baseApkPath': native_path}, process


class SemanticHookEvidenceTest(unittest.TestCase):
    def test_accepts_actual_protocol_two_plans_for_wechat_and_wecom(self):
        for app_id in ('wechat', 'wecom'):
            with self.subTest(app_id=app_id):
                candidate, module, processes, logs, installed, _ = lifecycle_fixture(app_id)
                diagnostics, lifecycle = derive_semantic_hook_evidence(
                    processes, logs, candidate, module, installed)
                self.assertEqual('OBSERVED', diagnostics['moduleLoaded'])
                self.assertEqual('COMPATIBLE', diagnostics['compatibility'])
                self.assertEqual('LOADED', lifecycle['nativeEvidence']['status'])
                self.assertEqual('INSTALLED', lifecycle['hookEvidence']['status'])
                self.assertEqual(candidate['expectedExecutionSha256'], lifecycle['hookEvidence']['executionSha256'])
                self.assertEqual(candidate['expectedPlan'], lifecycle['hookEvidence']['completePlan'])
                self.assertEqual({'wechat': 2, 'wecom': 3}[app_id], lifecycle['hookCount'])
                self.assertTrue(lifecycle['timingEvidence']['readyBeforeNative'])

    def test_qq_shared_decoder_binds_future_msf_process_and_qfix_evidence(self):
        candidate, module, processes, logs, _, process = lifecycle_fixture('qq')
        diagnostics, lifecycle = derive_semantic_hook_evidence(processes, logs, candidate, module)
        self.assertEqual('READY', lifecycle['qfixEvidence']['status'])
        self.assertNotIn('tinkerEvidence', lifecycle)
        self.assertEqual(process, lifecycle['hookEvidence']['processName'])
        self.assertEqual('INSTALLED_1', diagnostics['hookInstallation'])

    def test_rejects_pid_and_envelope_process_swaps(self):
        candidate, module, processes, logs, _, _ = lifecycle_fixture()
        corruptions = (
            logs.replace(' 3100 3100 I ImPad:', ' 3101 3101 I ImPad:', 1),
            logs.replace('(com.tencent.mm)', '(com.tencent.mm:worker)', 1),
        )
        for corrupted in corruptions:
            with self.subTest(corrupted=corrupted[:80]), self.assertRaises(ValueError):
                derive_semantic_hook_evidence(processes, corrupted, candidate, module)

    def test_rejects_missing_duplicate_restart_and_error_events(self):
        candidate, module, processes, logs, _, _ = lifecycle_fixture()
        lines = logs.rstrip().splitlines()
        for missing in lines:
            with self.subTest(missing=missing.split('event=')[1].split()[0]), self.assertRaises(ValueError):
                derive_semantic_hook_evidence(processes, '\n'.join(line for line in lines if line != missing) + '\n', candidate, module)
        with self.assertRaisesRegex(ValueError, 'exactly one'):
            derive_semantic_hook_evidence(processes, logs + lines[-1] + '\n', candidate, module)
        for marker in ('RESTART_REQUIRED initialization pending', 'semantic runtime unavailable: error', 'status=INCOMPATIBLE'):
            with self.subTest(marker=marker), self.assertRaises(ValueError):
                derive_semantic_hook_evidence(processes, logs + lines[0].split('] ', 1)[0] + '] ' + marker + '\n', candidate, module)

    def test_rejects_native_hash_source_and_load_path_mismatches(self):
        candidate, module, processes, logs, installed, _ = lifecycle_fixture()
        for old, new in ((f'apiSha256={API_SHA}', f'apiSha256={"e" * 64}'),
                         ('sourceApkPath=/data/app/impad/base.apk', 'sourceApkPath=/wrong/base.apk'),
                         ('nativeLoadPath=/data/app/impad/base.apk!/lib/arm64-v8a/libdexkit.so',
                          'nativeLoadPath=/wrong/libdexkit.so')):
            with self.subTest(old=old), self.assertRaises(ValueError):
                derive_semantic_hook_evidence(processes, logs.replace(old, new), candidate, module, installed)
        with self.assertRaisesRegex(ValueError, 'installed module'):
            derive_semantic_hook_evidence(processes, logs, candidate, module, {'baseApkPath': '/wrong/base.apk'})

    def test_rejects_protocol_timing_and_candidate_plan_or_source_mismatches(self):
        candidate, module, processes, logs, _, _ = lifecycle_fixture()
        swapped_plan = copy.deepcopy(candidate['expectedPlan'])
        swapped_plan['hooks'][0]['configuration']['mode'] = 'phone'
        swapped_payload = base64.urlsafe_b64encode(
            json.dumps(swapped_plan, separators=(',', ':')).encode()).decode().rstrip('=')
        encoded_plan = base64.urlsafe_b64encode(
            json.dumps(candidate['expectedPlan'], separators=(',', ':')).encode()).decode().rstrip('=')
        cases = (
            logs.replace('elapsedRealtimeNs=250', 'elapsedRealtimeNs=150'),
            logs.replace(f"executionSha256={candidate['expectedExecutionSha256']}", f"executionSha256={'f' * 64}", 1),
            logs.replace(f'plan={encoded_plan}', f'plan={swapped_payload}'),
        )
        for corrupted in cases:
            with self.subTest(corrupted=corrupted[-100:]), self.assertRaises(ValueError):
                derive_semantic_hook_evidence(processes, corrupted, candidate, module)
        changed = copy.deepcopy(candidate)
        changed['expectedPlan']['hooks'][0]['source']['dexSha256'] = 'f' * 64
        changed['expectedExecutionSha256'] = plan_execution_sha256(changed['expectedPlan'])
        with self.assertRaises(ValueError):
            derive_semantic_hook_evidence(processes, logs, changed, module)
        changed = copy.deepcopy(candidate)
        changed['expectedPlan']['hooks'][0]['configuration']['mode'] = 'phone'
        changed['expectedExecutionSha256'] = plan_execution_sha256(changed['expectedPlan'])
        with self.assertRaises(ValueError):
            derive_semantic_hook_evidence(processes, logs, changed, module)


if __name__ == '__main__':
    unittest.main()
