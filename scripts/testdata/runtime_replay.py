"""Small synthetic APK and runtime candidate bundles for replay tests."""
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import struct
import zipfile

from scripts.ci import candidate_selection
from scripts.ci.app_policy import get_policy
from scripts.ci.semantic_protocol import (catalog_sha256, execution_identity_sha256,
    plan_execution_sha256, snapshot_sha256)


ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent / 'runtime-replay'


def _source_rows(root, paths):
    return [
        {'path': path.relative_to(root).as_posix(),
         'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
        for path in sorted(set(paths), key=lambda item: item.relative_to(root).as_posix())
    ]


def current_module_fingerprint(root):
    root = Path(root).resolve()
    module_sources = candidate_selection.module_source_hashes(root)

    def kotlin_sources(directory):
        return list((root / directory).rglob('*.kt'))

    resolver_sources = _source_rows(
        root, kotlin_sources('compat-core/src/main/kotlin/io/github/nku100/impad/compat/rules'))
    android_resolver_paths = [root / path for path in (
        'app/src/main/kotlin/io/github/nku100/impad/runtime/TargetMethodResolver.kt',
        'app/src/main/kotlin/io/github/nku100/impad/runtime/RuleRuntimeResolver.kt',
        'app/src/main/kotlin/io/github/nku100/impad/runtime/RuleResolutionCache.kt',
        'app/src/main/kotlin/io/github/nku100/impad/runtime/InstalledBuildIdentityReader.kt',
        'app/src/main/kotlin/io/github/nku100/impad/runtime/AndroidDexKitRuntime.kt',
        'app/src/main/kotlin/io/github/nku100/impad/runtime/RuleInstallCoordinator.kt',
        'app/src/main/kotlin/io/github/nku100/impad/ImPadModule.kt',
    )]
    resolver_sources.extend(_source_rows(root, android_resolver_paths))
    strategy_paths = (kotlin_sources('app/src/main/kotlin/io/github/nku100/impad/apps/qq')
                      + kotlin_sources('app/src/main/kotlin/io/github/nku100/impad/apps/wecom')
                      + [root / path for path in (
                          'app/src/main/kotlin/io/github/nku100/impad/runtime/HookInstaller.kt',
                          'app/src/main/kotlin/io/github/nku100/impad/runtime/HookCall.kt',
                      )])
    lock_bytes = (root / 'gradle/dexkit-lock.json').read_bytes()
    lock = json.loads(lock_bytes)
    artifacts = lock['artifacts']
    return {
        'schemaVersion': 1,
        'ruleSources': _source_rows(root, kotlin_sources('compat-rules/src/main/kotlin')),
        'resolverSources': resolver_sources,
        'strategySources': _source_rows(root, strategy_paths),
        'moduleSources': module_sources,
        'sourceTreeSha256': candidate_selection.source_tree_sha256(root),
        'routeConfigurationSha256': candidate_selection.route_configuration_sha256(module_sources),
        'dependencyLockSha256': candidate_selection.dependency_lock_sha256(module_sources),
        'dexkitLockSha256': hashlib.sha256(lock_bytes).hexdigest(),
        'dexkitSourceCommit': lock['source']['commit'],
        'dexkitApiSha256': artifacts['jvmApi']['sha256'],
        'dexkitAndroidApiSha256': artifacts['androidAar']['sha256'],
        'dexkitLinuxAarch64NativeSha256': artifacts['linuxAarch64']['sha256'],
        'dexkitLinuxX8664NativeSha256': artifacts['linuxX8664']['sha256'],
        'dexkitAndroidArm64NativeSha256': artifacts['androidArm64V8a']['sha256'],
    }


def _android_tools():
    sdk_value = os.environ.get('ANDROID_HOME') or os.environ.get('ANDROID_SDK_ROOT')
    if not sdk_value:
        raise RuntimeError('Android SDK is required to construct the synthetic manifest APK')
    sdk = Path(sdk_value)
    build_tools = sorted((sdk / 'build-tools').glob('*/aapt'))
    platforms = sorted((sdk / 'platforms').glob('*/android.jar'))
    if not build_tools or not platforms:
        raise RuntimeError('Android SDK build-tools/aapt and platform android.jar are required')
    return build_tools[-1], platforms[-1]


def _write_manifest_apk(app_id, identity, root):
    root.mkdir(parents=True, exist_ok=True)
    manifest = root / 'AndroidManifest.xml'
    package = identity['packageName']
    version = identity['versionName']
    version_code = identity['versionCode']
    metadata = ''
    if app_id == 'qq':
        metadata = ('<meta-data android:name="AppSetting_params" android:value="10"/>'
                    '<meta-data android:name="AppSetting_params_pad" android:value="20"/>')
    manifest.write_text(
        '<manifest xmlns:android="http://schemas.android.com/apk/res/android" '
        f'package="{package}" android:versionCode="{version_code}" '
        f'android:versionName="{version}"><application>{metadata}</application></manifest>\n')
    aapt, android_jar = _android_tools()
    apk = root / 'fixture.apk'
    subprocess.run([str(aapt), 'package', '-f', '-M', str(manifest), '-F', str(apk),
                    '-I', str(android_jar)], check=True, capture_output=True, text=True)
    return apk


def _synthetic_static_report(app_id, identity, input_path, input_hash, expected_plan,
                             fingerprint, fingerprint_sha):
    """Build a structurally complete schema 2 report for test plumbing only."""
    expected_targets = expected_plan['targets']
    roles = list(expected_targets)
    recognizer_id = f'synthetic_{app_id}_recognizer'
    strategy_id = expected_plan['strategyId']
    catalog = {
        'schemaVersion': 2, 'appId': app_id, 'ruleCatalogSha256': '',
        'contractId': f'synthetic_{app_id}_contract', 'contractRevision': 1,
        'requiredRoles': roles, 'recognizerIds': [recognizer_id],
        'strategies': [{'id': strategy_id, 'revision': 1, 'roles': [
            {'role': role, 'phase': 'AROUND', 'configuration': {}} for role in roles]}],
        'versionHints': [],
    }
    catalog['ruleCatalogSha256'] = catalog_sha256(catalog)

    dex_sha = hashlib.sha256(f'synthetic-{app_id}-dex'.encode()).hexdigest()
    snapshot = {
        'protocolVersion': 1, 'readyLoaderId': 'synthetic-loader', 'contentSha256': '',
        'loaders': [{'id': 'synthetic-loader', 'parentId': None,
                     'delegation': 'PARENT_FIRST', 'dexSha256': [dex_sha]}],
        'archives': [{'loaderId': 'synthetic-loader', 'containerIndex': 0, 'kind': 'ZIP',
                      'containerSha256': input_hash, 'dexSha256': [dex_sha]}],
    }
    snapshot['contentSha256'] = snapshot_sha256(snapshot)
    plan = {
        'schemaVersion': 1, 'snapshotSha256': snapshot['contentSha256'],
        'contractId': catalog['contractId'], 'contractRevision': catalog['contractRevision'],
        'strategyId': strategy_id, 'strategyRevision': 1,
        'hooks': [{
            'role': role, **expected_targets[role],
            'source': {'loaderId': 'synthetic-loader', 'dexIndex': 0, 'dexSha256': dex_sha},
            'phase': 'AROUND', 'configuration': {},
        } for role in roles],
    }
    plan_sha = plan_execution_sha256(plan)
    execution = {
        'ruleCatalogSha256': catalog['ruleCatalogSha256'],
        'ruleImplementationSha256': hashlib.sha256(b'synthetic rule implementation').hexdigest(),
        'sharedResolverImplementationSha256': hashlib.sha256(b'synthetic resolver implementation').hexdigest(),
        'dexkitLockSha256': fingerprint['dexkitLockSha256'],
        'dexkitSourceCommit': fingerprint['dexkitSourceCommit'],
        'dexkitApiSha256': fingerprint['dexkitApiSha256'],
        'dexkitNativeSha256': None, 'dexkitPlatform': None,
        'dexkitAndroidApiSha256': fingerprint['dexkitAndroidApiSha256'],
        'dexkitLinuxAarch64NativeSha256': fingerprint['dexkitLinuxAarch64NativeSha256'],
        'dexkitLinuxX8664NativeSha256': fingerprint['dexkitLinuxX8664NativeSha256'],
        'dexkitAndroidArm64NativeSha256': fingerprint['dexkitAndroidArm64NativeSha256'],
        'strategyImplementationSha256': hashlib.sha256(b'synthetic strategy implementation').hexdigest(),
        'sourceTreeSha256': fingerprint['sourceTreeSha256'],
        'routeConfigurationSha256': fingerprint['routeConfigurationSha256'],
        'dependencyLockSha256': fingerprint['dependencyLockSha256'],
        'fingerprintFileSha256': fingerprint_sha,
    }
    execution['cacheIdentitySha256'] = execution_identity_sha256(execution)
    entry = {'plan': plan, 'executionSha256': plan_sha,
             'evidence': ['synthetic test fixture']}
    recognizer = {'recognizerId': recognizer_id, 'status': 'PLANS',
                  'plans': [entry], 'diagnostics': [], 'reason': None}
    selected = {
        'status': 'SELECTED', 'plan': plan, 'executionSha256': plan_sha,
        'evidenceByRecognizer': {recognizer_id: ['synthetic test fixture']},
        'alternatives': [], 'diagnostics': {}, 'incompleteReasons': {},
    }
    return {
        'schemaVersion': 2, 'analysisScope': 'APK_FILE_SNAPSHOT', 'appId': app_id,
        'identity': identity, 'inputHashes': [{'path': str(input_path), 'sha256': input_hash}],
        'ruleCatalog': catalog, 'executionIdentity': execution, 'snapshot': snapshot,
        'evaluatedRecognizerIds': [recognizer_id], 'recognizers': [recognizer],
        'selection': selected,
    }


def synthetic_qq_lifecycle_lines(candidate, module):
    """Return newly constructed log lines for synthetic validator plumbing."""
    identity = module['dexkit']
    plan = candidate['expectedPlan']
    encoded_plan = base64.urlsafe_b64encode(
        json.dumps(plan, separators=(',', ':')).encode()).decode().rstrip('=')
    execution_sha = candidate['expectedExecutionSha256']
    catalog = candidate['ruleCatalog']
    prefix = ('07-01 12:00:00.{millis}  1905  1905 I LSPosedFramework: '
              '(com.tencent.mobileqq:MSF)[io.github.nku100.impad,ImPad,synthetic,1,1] ')
    return [
        prefix.format(millis='300') +
        'rule-runtime event=qfix_ready elapsedRealtimeNs=100 '
        'contextPackage=com.tencent.mobileqq loaderClass=dalvik.system.PathClassLoader',
        prefix.format(millis='400') +
        'rule-runtime event=native_loaded elapsedRealtimeNs=200 '
        f"apiSha256={identity['dexkitAndroidApiSha256']} "
        f"nativeSha256={identity['dexkitAndroidArm64NativeSha256']} nativeAbi=arm64-v8a "
        'packagingMode=APK_DIRECT sourceApkPath=/data/app/io.github.nku100.impad/base.apk '
        'nativeLoadPath=/data/app/io.github.nku100.impad/base.apk!/lib/arm64-v8a/libdexkit.so',
        prefix.format(millis='450') +
        'rule-runtime event=semantic_plan protocolVersion=2 elapsedRealtimeNs=250 '
        f'executionSha256={execution_sha} plan={encoded_plan} cacheHit=false attempts=1',
        prefix.format(millis='500') +
        'rule-runtime event=hook_installed protocolVersion=2 elapsedRealtimeNs=300 '
        f"contractId={catalog['contractId']} contractRevision={catalog['contractRevision']} "
        f"strategyId={plan['strategyId']} strategyRevision={plan['strategyRevision']} "
        f"executionSha256={execution_sha} snapshotSha256={plan['snapshotSha256']} "
        f"hookCount={len(plan['hooks'])} "
        'processName=com.tencent.mobileqq:MSF',
    ]


def prepare_candidate_bundle(app_id, directory, repository=ROOT):
    """Materialize a current-source test module and a tiny manifest-only app APK."""
    directory = Path(directory).resolve()
    template = FIXTURES / app_id
    candidate = json.loads((template / 'candidate-template.json').read_text())
    # The frozen schema 1 report remains historical replay input. Production-path
    # integration tests get a separate complete synthetic schema 2 document.
    candidate['identity']['signerSha256'] = get_policy(app_id).signer_sha256
    candidate_root = directory / 'candidate'
    apk_path = _write_manifest_apk(app_id, candidate['identity'],
                                   candidate_root / 'apk-inputs')
    digest = hashlib.sha256(apk_path.read_bytes()).hexdigest()
    candidate['identity']['apkSha256'] = digest
    candidate['apkFiles'][0]['sha256'] = digest
    if app_id == 'qq':
        candidate['requestIdentityExpectation']['apkSha256'] = digest

    fingerprint = current_module_fingerprint(repository)
    fingerprint_bytes = (json.dumps(fingerprint, indent=2) + '\n').encode()
    module_apk = candidate_root / 'module/app-debug.apk'
    module_apk.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(module_apk, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('assets/runtime-identity.json', fingerprint_bytes)
        archive.writestr('classes.dex', b'dex\n035\0')

    static = _synthetic_static_report(
        app_id, candidate['identity'], 'apk-inputs/fixture.apk', digest, candidate['expectedPlan'],
        fingerprint, hashlib.sha256(fingerprint_bytes).hexdigest())
    static_path = candidate_root / candidate['staticReport']
    static_path.parent.mkdir(parents=True, exist_ok=True)
    static_path.write_text(json.dumps(static, indent=2) + '\n')
    recognition, recognition_sha = candidate_selection.recognition_identity(static)
    candidate = {
        'schemaVersion': 2, 'appId': app_id, 'status': 'PREPARED',
        'identity': candidate['identity'],
        'sourceUrl': f'synthetic://{app_id}/manifest-only.apk',
        'apkFiles': [{'path': 'apk-inputs/fixture.apk', 'role': 'base', 'sha256': digest}],
        'ruleCatalog': static['ruleCatalog'], 'snapshot': static['snapshot'],
        'ruleCatalogSha256': static['ruleCatalog']['ruleCatalogSha256'],
        'recognition': recognition, 'recognitionSha256': recognition_sha,
        'staticReport': 'static-check.json',
        'staticReportSha256': hashlib.sha256(static_path.read_bytes()).hexdigest(),
        'expectedPlan': static['selection']['plan'],
        'expectedExecutionSha256': static['selection']['executionSha256'],
    }
    if app_id == 'qq':
        from scripts.ci.apps.qq.runtime_probe import manifest_expectation
        candidate['requestIdentityExpectation'] = manifest_expectation(apk_path, digest)
    (candidate_root / 'synthetic-fixture-provenance.json').write_text(json.dumps({
        'kind': 'SYNTHETIC_TEST_ONLY',
        'purpose': 'Exercise schema 2 candidate and runtime validation integration.',
        'deviceBusinessValidation': False,
        'historicalSchema1FixturesUsedAsSemanticEvidence': False,
    }, indent=2) + '\n')
    candidate['module'] = candidate_selection.prepare_module(module_apk, static, repository)
    candidate_path = candidate_root / 'candidate-report.json'
    candidate_path.write_text(json.dumps(candidate, indent=2) + '\n')
    return candidate_root, candidate_path, candidate, static, module_apk


def qq_packet(app_id, sequence, command):
    def field(value):
        return struct.pack('>I', len(value) + 4) + value

    header = (struct.pack('>III', sequence, app_id, 2052) + bytes(12)
              + field(b'') + field(command.encode('ascii')))
    body = field(header) + field(b'')
    outer = struct.pack('>IB', 12, 0) + field(b'') + b'\0' + field(b'0') + body
    return field(outer)
