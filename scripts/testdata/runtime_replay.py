"""Small synthetic APK and runtime candidate bundles for replay tests."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import struct
import zipfile

from scripts.ci import candidate_selection
from scripts.ci.app_policy import get_policy


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


def prepare_candidate_bundle(app_id, directory, repository=ROOT):
    """Materialize a current-source test module and a tiny manifest-only app APK."""
    directory = Path(directory).resolve()
    template = FIXTURES / app_id
    candidate = json.loads((template / 'candidate-template.json').read_text())
    static = json.loads((template / 'static-template.json').read_text())
    candidate['identity']['signerSha256'] = get_policy(app_id).signer_sha256
    candidate_root = directory / 'candidate'
    apk_path = _write_manifest_apk(app_id, candidate['identity'],
                                   candidate_root / 'apk-inputs')
    digest = hashlib.sha256(apk_path.read_bytes()).hexdigest()
    candidate['identity']['apkSha256'] = digest
    candidate['apkFiles'][0]['sha256'] = digest
    static['identity'] = candidate['identity']
    static['inputHashes'] = [{'path': 'apk-inputs/fixture.apk', 'sha256': digest}]
    if app_id == 'qq':
        candidate['requestIdentityExpectation']['apkSha256'] = digest

    fingerprint = current_module_fingerprint(repository)
    fingerprint_bytes = (json.dumps(fingerprint, indent=2) + '\n').encode()
    module_apk = candidate_root / 'module/app-debug.apk'
    module_apk.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(module_apk, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('assets/runtime-identity.json', fingerprint_bytes)
        archive.writestr('classes.dex', b'dex\n035\0')

    execution = static['executionIdentity']
    execution.update(
        fingerprintFileSha256=hashlib.sha256(fingerprint_bytes).hexdigest(),
        sourceTreeSha256=fingerprint['sourceTreeSha256'],
        routeConfigurationSha256=fingerprint['routeConfigurationSha256'],
        dependencyLockSha256=fingerprint['dependencyLockSha256'],
    )
    static_path = candidate_root / candidate['staticReport']
    static_path.parent.mkdir(parents=True, exist_ok=True)
    static_path.write_text(json.dumps(static, indent=2) + '\n')
    candidate['staticReportSha256'] = hashlib.sha256(static_path.read_bytes()).hexdigest()
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
