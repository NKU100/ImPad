"""Prepare exact official application inputs for native hosted ReDroid acceptance."""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

from scripts.ci.app_policy import get_policy, validate_targets, policy_for_report
from scripts.ci.static_regression import ensure_apk, check_apk
from scripts.ci.apps.qq.runtime_probe import manifest_expectation
from scripts.ci.apps.qq.prepare_probe import prepare


def verify_signer(output, expected):
    signers = re.findall(r'^(?:Signer #\d+|V[123](?:\.[01])? Signer:) certificate SHA-256 digest: ([a-fA-F0-9]{64})$', output, re.M)
    if [value.lower() for value in signers] != [expected.lower()]:
        raise ValueError('Official APK signer mismatch or multiple signers')


def write_profiles(source, root):
    inputs, packages = {}, []
    for app in ('wechat', 'wecom', 'qq'):
        profiles = json.loads((source / (app + '.json')).read_text())
        validate_targets(profiles, app)
        local = copy.deepcopy(profiles)
        for profile in local:
            profile.pop('sourceUrl', None)
            profile['verificationStatus'] = 'static-verified'
        target = root / 'assets/compatibility-local' / app / 'targets.json'
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(local, indent=2) + '\n')
        packages.append(get_policy(app).package_name)
        for index, profile in enumerate(profiles):
            name = 'qq-old' if app == 'qq' and index == 0 else app
            inputs[name] = copy.deepcopy(profile)
    scope = root / 'resources/META-INF/xposed/scope.list'
    scope.parent.mkdir(parents=True, exist_ok=True)
    scope.write_text('\n'.join(packages) + '\n')
    return inputs


def write_candidate(report, root):
    policy = policy_for_report(report, report.get('appId'))
    if report.get('status') != 'STATIC_VERIFIED_PENDING_RUNTIME':
        raise ValueError('Candidate requires static verification')
    profile = copy.deepcopy(report['suggestedProfile'])
    if profile['identity'] != report['identity']:
        raise ValueError('Candidate profile identity mismatch')
    validate_targets([profile], policy.app_id)
    profile.pop('sourceUrl', None)
    profile['verificationStatus'] = 'static-verified'
    target = root / 'assets/compatibility-local' / policy.app_id / 'targets.json'
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps([profile], indent=2) + '\n')
    scope = root / 'resources/META-INF/xposed/scope.list'
    scope.parent.mkdir(parents=True, exist_ok=True)
    scope.write_text(policy.package_name + '\n')
    return {policy.app_id: copy.deepcopy(report['suggestedProfile'])}


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
    source = Path('environment/redroid-smoke').resolve()
    if args.tools_only:
        prepare_tools(root)
        return
    report = json.loads(args.candidate.read_text()) if args.candidate else None
    inputs = write_candidate(report, root) if report else write_profiles(source, root)
    sdk = Path(os.environ.get('ANDROID_HOME') or os.environ['ANDROID_SDK_ROOT'])
    for name, profile in inputs.items():
        app = 'qq' if name == 'qq-old' else name
        if report:
            candidates = [p for p in root.glob('*.apk') if hashlib.sha256(p.read_bytes()).hexdigest() == profile['identity']['apkSha256']]
            if len(candidates) != 1:
                raise ValueError('Candidate artifact requires exactly one matching APK')
            apk = candidates[0]
            target = root / 'candidate-targets.json'
            target.write_text(json.dumps([profile]))
        else:
            apk = ensure_apk(profile, root / 'apk-cache', app)
            target = source / (app + '.json')
        signer = subprocess.check_output([str(sdk / 'build-tools/37.0.0/apksigner'), 'verify', '--print-certs', str(apk)], timeout=120).decode()
        (root / (name + '-signer.txt')).write_text(signer)
        verify_signer(signer, profile['identity']['signerSha256'])
        check_apk(Path.cwd(), target, apk, profile['identity']['apkSha256'], root / (name + '-static.txt'), app_id=app)
        if apk.resolve() != (root / (name + '.apk')).resolve():
            shutil.copyfile(apk, root / (name + '.apk'))
        profile['signerVerified'] = True
        if app == 'qq':
            profile['manifestAppIds'] = manifest_expectation(apk, profile['identity']['apkSha256'])
    (root / 'inputs.json').write_text(json.dumps(inputs, indent=2) + '\n')
    prepare_tools(root)


if __name__ == '__main__':
    main()
