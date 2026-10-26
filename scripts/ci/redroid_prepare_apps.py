"""Prepare exact official application inputs for native hosted ReDroid acceptance."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

from scripts.ci.app_policy import get_policy, policy_for_report
from scripts.ci.static_regression import check_apk
from scripts.ci.apps.qq.runtime_probe import manifest_expectation
from scripts.ci.apps.qq.prepare_probe import prepare
from scripts.ci.candidate_selection import prepare_module
from scripts.ci.apk_inputs import resolve_apk_inputs


def verify_signer(output, expected):
    signers = re.findall(r'^(?:Signer #\d+|V[123](?:\.[01])? Signer:) certificate SHA-256 digest: ([a-fA-F0-9]{64})$', output, re.M)
    if [value.lower() for value in signers] != [expected.lower()]:
        raise ValueError('Official APK signer mismatch or multiple signers')


def write_candidate(report, root):
    policy = policy_for_report(report, report.get('appId'))
    if report.get('schemaVersion') != 1 or report.get('status') != 'PREPARED':
        raise ValueError('Runtime candidate requires the shared prepared-candidate schema')
    if (report.get('expectedPlan', {}).get('selection') != 'UNIQUE'
            or report['identity'].get('apkSha256') != report.get('apkFiles', [{}])[0].get('sha256')):
        raise ValueError('Candidate identity or unique rule plan is incomplete')
    scope = root / 'resources/META-INF/xposed/scope.list'
    scope.parent.mkdir(parents=True, exist_ok=True)
    scope.write_text(policy.package_name + '\n')
    return {policy.app_id: report}


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
    report = json.loads(args.candidate.read_text())
    static_path = (args.candidate.parent / report.get('staticReport', '')).resolve()
    static = json.loads(static_path.read_text())
    module_path = root / 'module/app-debug.apk'
    module_identity = prepare_module(module_path, static, Path.cwd())
    report['module'] = module_identity
    (root / 'module-identity.json').write_text(json.dumps(module_identity, indent=2) + '\n')
    args.candidate.write_text(json.dumps(report, indent=2) + '\n')
    inputs = write_candidate(report, root)
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
