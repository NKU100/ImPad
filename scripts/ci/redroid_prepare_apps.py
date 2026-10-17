"""Prepare exact official application inputs for native hosted ReDroid acceptance."""
import argparse
import copy
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

from scripts.ci.app_policy import get_policy, validate_targets
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    source = Path('environment/redroid-smoke').resolve()
    inputs = write_profiles(source, root)
    sdk = Path(os.environ.get('ANDROID_HOME') or os.environ['ANDROID_SDK_ROOT'])
    for name, profile in inputs.items():
        app = 'qq' if name == 'qq-old' else name
        apk = ensure_apk(profile, root / 'apk-cache', app)
        signer = subprocess.check_output([str(sdk / 'build-tools/37.0.0/apksigner'), 'verify', '--print-certs', str(apk)], timeout=120).decode()
        (root / (name + '-signer.txt')).write_text(signer)
        verify_signer(signer, profile['identity']['signerSha256'])
        check_apk(Path.cwd(), source / (app + '.json'), apk, profile['identity']['apkSha256'], root / (name + '-static.txt'), app_id=app)
        shutil.copyfile(apk, root / (name + '.apk'))
        profile['signerVerified'] = True
        if app == 'qq':
            profile['manifestAppIds'] = manifest_expectation(apk, profile['identity']['apkSha256'])
    (root / 'inputs.json').write_text(json.dumps(inputs, indent=2) + '\n')
    prepare(root, 'arm64')
    classes = root / 'ui-classes'
    classes.mkdir(exist_ok=True)
    android = sdk / 'platforms/android-37.2/android.jar'
    subprocess.run(['javac', '--release', '17', '-cp', str(android), '-d', str(classes), 'scripts/ci/UiHierarchy.java'], check=True, timeout=120)
    subprocess.run([str(sdk / 'build-tools/37.0.0/d8'), '--min-api', '28', '--lib', str(android), '--output', str(root / 'ui-hierarchy.jar'), str(classes / 'UiHierarchy.class')], check=True, timeout=120)


if __name__ == '__main__':
    main()
