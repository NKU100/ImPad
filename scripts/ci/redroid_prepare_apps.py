"""Prepare exact official application inputs for native hosted ReDroid acceptance."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from scripts.ci.app_policy import get_policy, policy_for_report
from scripts.ci.static_regression import check_apk
from scripts.ci.apps.qq.runtime_probe import manifest_expectation
from scripts.ci.apps.qq.prepare_probe import prepare
from scripts.ci.candidate_selection import prepare_module, recognition_identity
from scripts.ci.apk_inputs import resolve_apk_inputs
from scripts.ci.semantic_protocol import validate_check_report


_SHA256 = re.compile(r'[a-fA-F0-9]{64}')


def _resolve_artifact(root, reference, label):
    if not isinstance(reference, str) or not reference or Path(reference).is_absolute():
        raise ValueError(f'{label} path must be relative to the candidate artifact root')
    path = (root / reference).resolve()
    if not path.is_relative_to(root):
        raise ValueError(f'{label} path escapes the candidate artifact root')
    return path


def _file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def validate_prepared_candidate(report, root, static_report=None, module_identity=None):
    """Validate schema2 semantic evidence and the exact files it names."""
    root = Path(root).resolve()
    if (not isinstance(report, dict) or type(report.get('schemaVersion')) is not int
            or report.get('schemaVersion') != 2):
        raise ValueError('Runtime candidate must use schema version 2')
    if report.get('status') != 'PREPARED':
        raise ValueError('Runtime candidate must be PREPARED')
    policy = policy_for_report(report, report.get('appId'))
    identity = report.get('identity')
    if not isinstance(identity, dict):
        raise ValueError('Candidate APK identity is missing')
    static_path = _resolve_artifact(root, report.get('staticReport'), 'Static report')
    if not static_path.is_file() or _file_sha256(static_path) != report.get('staticReportSha256'):
        raise ValueError('Static checker report identity is missing or changed')
    stored_static_report = json.loads(static_path.read_text())
    if static_report is not None and static_report != stored_static_report:
        raise ValueError('Loaded static checker report differs from its artifact bytes')
    static_report = stored_static_report
    validate_check_report(static_report, expected_app_id=policy.app_id,
                          expected_identity=identity)

    selection = static_report['selection']
    if selection['status'] != 'SELECTED' or selection['plan'] is None:
        raise ValueError('Static checker did not select one complete semantic plan')
    expected_plan = selection['plan']
    if (report.get('ruleCatalog') != static_report['ruleCatalog']
            or report.get('ruleCatalogSha256') != static_report['ruleCatalog']['ruleCatalogSha256']
            or report.get('snapshot') != static_report['snapshot']
            or report.get('expectedPlan') != expected_plan
            or report.get('expectedExecutionSha256') != selection['executionSha256']):
        raise ValueError('Prepared candidate differs from the selected complete semantic plan')
    recognition, recognition_sha = recognition_identity(static_report)
    if (report.get('recognition') != recognition
            or report.get('recognitionSha256') != recognition_sha):
        raise ValueError('Prepared candidate recognition identity differs from static evidence')

    apk_files = report.get('apkFiles')
    input_hashes = static_report['inputHashes']
    if not isinstance(apk_files, list) or not apk_files or len(apk_files) != len(input_hashes):
        raise ValueError('Candidate APK files do not cover every static checker input')
    for index, (apk_file, static_input) in enumerate(zip(apk_files, input_hashes)):
        role = 'base' if index == 0 else 'split'
        if (not isinstance(apk_file, dict) or apk_file.get('role') != role
                or not isinstance(static_input, dict)
                or not isinstance(apk_file.get('sha256'), str)
                or not _SHA256.fullmatch(apk_file['sha256'])
                or apk_file['sha256'].lower() != static_input.get('sha256', '').lower()):
            raise ValueError('Candidate APK hashes differ from static checker input identities')
        apk_path = _resolve_artifact(root, apk_file.get('path'), 'Candidate APK')
        static_input_path = _resolve_artifact(root, static_input.get('path'), 'Static checker APK input')
        if apk_path != static_input_path or not apk_path.is_file():
            raise ValueError('Candidate APK path differs from the actual static checker input')
        if _file_sha256(apk_path) != apk_file['sha256'].lower():
            raise ValueError('Candidate APK bytes differ from static checker input identities')
    if identity.get('apkSha256', '').lower() != apk_files[0]['sha256'].lower():
        raise ValueError('Candidate identity does not match the exact base APK')

    module = module_identity if module_identity is not None else report.get('module')
    if module is None and module_identity is None:
        return {policy.app_id: report}
    if not isinstance(module, dict) or type(module.get('schemaVersion')) is not int or module['schemaVersion'] != 2:
        raise ValueError('Prepared module identity must use schema version 2')
    if module_identity is not None and report.get('module') != module_identity:
        raise ValueError('Candidate module identity differs from the prepared module')
    if (not isinstance(module.get('sha256'), str) or not _SHA256.fullmatch(module['sha256'])
            or module.get('recognition') != recognition
            or module.get('recognitionSha256') != recognition_sha
            or module.get('ruleCatalogSha256') != static_report['ruleCatalog']['ruleCatalogSha256']
            or module.get('dexkit') != static_report['executionIdentity']):
        raise ValueError('Prepared module recognition or catalog identity differs from static evidence')
    execution = static_report['executionIdentity']
    for key in ('sourceTreeSha256', 'dependencyLockSha256',
                'routeConfigurationSha256', 'fingerprintFileSha256'):
        if (not isinstance(execution.get(key), str) or not _SHA256.fullmatch(execution[key])
                or module.get(key) != execution[key]):
            raise ValueError(f'Prepared module {key} differs from static evidence')
    return {policy.app_id: report}


def _atomic_write(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                     prefix='.' + path.name + '-', delete=False) as output:
        output.write(content)
        temporary = Path(output.name)
    temporary.replace(path)


def verify_signer(output, expected):
    signers = re.findall(r'^(?:Signer #\d+|V[123](?:\.[01])? Signer:) certificate SHA-256 digest: ([a-fA-F0-9]{64})$', output, re.M)
    if [value.lower() for value in signers] != [expected.lower()]:
        raise ValueError('Official APK signer mismatch or multiple signers')


def write_candidate(report, root, *, static_report=None, module_identity=None):
    root = Path(root).resolve()
    inputs = validate_prepared_candidate(report, root, static_report, module_identity)
    policy = policy_for_report(report, report.get('appId'))
    scope = root / 'resources/META-INF/xposed/scope.list'
    scope.parent.mkdir(parents=True, exist_ok=True)
    scope.write_text(policy.package_name + '\n')
    return inputs


def prepare_tools(root):
    prepare(root, 'arm64')
    sdk = Path(os.environ.get('ANDROID_HOME') or os.environ['ANDROID_SDK_ROOT'])
    classes = root / 'ui-classes'
    classes.mkdir(exist_ok=True)
    android = sdk / 'platforms/android-37.2/android.jar'
    subprocess.run(['javac', '--release', '17', '-cp', str(android), '-d', str(classes), 'scripts/ci/UiHierarchy.java'], check=True, timeout=120)
    subprocess.run([str(sdk / 'build-tools/37.0.0/d8'), '--min-api', '28', '--lib', str(android), '--output', str(root / 'ui-hierarchy.jar'), str(classes / 'UiHierarchy.class')], check=True, timeout=120)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--candidate', type=Path)
    parser.add_argument('--tools-only', action='store_true')
    args = parser.parse_args()
    root = args.root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    if args.tools_only:
        prepare_tools(root)
        return
    if args.candidate is None:
        parser.error('--candidate is required unless --tools-only is used')
    candidate_path = args.candidate.resolve()
    if not candidate_path.is_relative_to(root):
        raise ValueError('Candidate report must be inside the runtime artifact root')
    report = json.loads(candidate_path.read_text())
    if (not isinstance(report, dict) or type(report.get('schemaVersion')) is not int
            or report.get('schemaVersion') != 2):
        raise ValueError('Runtime candidate must use schema version 2')
    static_path = _resolve_artifact(root, report.get('staticReport'), 'Static report')
    static = json.loads(static_path.read_text())
    validate_prepared_candidate(report, root, static)
    module_path = root / 'module/app-debug.apk'
    module_identity = prepare_module(module_path, static, Path.cwd())
    prepared = dict(report, module=module_identity)
    inputs = validate_prepared_candidate(prepared, root, static, module_identity)
    write_candidate(prepared, root, static_report=static, module_identity=module_identity)
    _atomic_write(root / 'module-identity.json', json.dumps(module_identity, indent=2) + '\n')
    _atomic_write(candidate_path, json.dumps(prepared, indent=2) + '\n')
    report = prepared
    sdk = Path(os.environ.get('ANDROID_HOME') or os.environ['ANDROID_SDK_ROOT'])
    for name, candidate in inputs.items():
        app = name
        identity = candidate['identity']
        candidates = [path for path in root.glob('*.apk')
                      if hashlib.sha256(path.read_bytes()).hexdigest() == identity['apkSha256']]
        if len(candidates) != 1:
            raise ValueError('Candidate artifact requires exactly one matching APK')
        apk = candidates[0]
        apk_inputs = resolve_apk_inputs(candidate, root)
        policy = policy_for_report(candidate, candidate.get('appId'))
        for index, path in enumerate(apk_inputs):
            signer = subprocess.check_output([str(sdk / 'build-tools/37.0.0/apksigner'), 'verify', '--print-certs', str(path)], timeout=120).decode()
            (root / f'{name}-signer-{index}.txt').write_text(signer)
            verify_signer(signer, policy.signer_sha256)
        if identity['signerSha256'].lower() != policy.signer_sha256:
            raise ValueError('Candidate signer differs from the trusted application signer')
        options = {'abi_splits': apk_inputs[1:]} if len(apk_inputs) > 1 else {}
        check_apk(Path.cwd(), apk, identity['apkSha256'], root / (name + '-static.txt'), app_id=app, **options)
        if apk.resolve() != (root / (name + '.apk')).resolve():
            shutil.copyfile(apk, root / (name + '.apk'))
        candidate['signerVerified'] = True
        if app == 'qq':
            candidate['requestIdentityExpectation'] = manifest_expectation(apk, identity['apkSha256'])
    (root / 'inputs.json').write_text(json.dumps(inputs, indent=2) + '\n')
    prepare_tools(root)


if __name__ == '__main__':
    main()
