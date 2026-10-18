"""Run the shared checker on exact, digest-verified official APKs."""
import argparse
from http.client import HTTPException
import json
import re
import shlex
import subprocess
from pathlib import Path

from scripts.ci.app_policy import get_policy, validate_targets
from scripts.ci.discover_latest_wechat import validate_official_apk_url
from scripts.ci.fetch_latest_wechat_apk import download_candidate, file_sha256, remote_metadata

SUPPORTED = {'runtime-verified-local', 'runtime-verified-hosted'}


def registered_window(profiles, app_id='wechat'):
    policy = get_policy(app_id)
    validate_targets(profiles, policy.app_id)
    identities = [p.get('identity', {}) for p in profiles
                  if p.get('identity', {}).get('packageName') == policy.package_name]
    abis = {identity.get('abi') for identity in identities if identity.get('abi')}
    if len(abis) != 1:
        raise ValueError('Static regression requires one registered ABI for the app package')
    abi = next(iter(abis))
    supported = [p for p in profiles if p.get('verificationStatus') in SUPPORTED
                 and p.get('identity', {}).get('packageName') == policy.package_name
                 and p.get('identity', {}).get('abi') == abi]
    recent_codes = sorted({p['identity']['versionCode'] for p in supported})[-3:]
    selected_codes = set(recent_codes)
    selected = [p for p in supported if p['identity']['versionCode'] in selected_codes]
    selected.sort(key=lambda p: p['identity']['versionCode'], reverse=True)
    if not selected:
        raise ValueError('Static regression requires at least one runtime-verified profile')
    return selected


def ensure_apk(profile, cache, app_id='wechat'):
    policy = get_policy(app_id)
    if profile.get('identity', {}).get('packageName') != policy.package_name:
        raise ValueError('Regression profile package does not match app policy')
    validate_official_apk_url(profile['sourceUrl'], policy.official_apk_prefix)
    digest = profile['identity']['apkSha256'].lower()
    if not re.fullmatch(r'[a-f0-9]{64}', digest):
        raise ValueError('Registered APK SHA-256 must contain 64 hexadecimal characters')
    path = cache / (digest + '.apk')
    if not path.is_file() or file_sha256(path) != digest:
        path, _, _, _ = download_candidate(profile['sourceUrl'], cache, digest,
                                           remote_metadata(profile['sourceUrl'], policy), policy)
    if file_sha256(path) != digest:
        raise ValueError('Regression APK hash does not match the registered profile')
    return path


def check_apk(repository, targets, apk, digest, log, offline=False, app_id='wechat'):
    apk = apk.resolve()
    if targets.is_file():
        validate_targets(json.loads(targets.read_text()), app_id)
    if file_sha256(apk) != digest.lower():
        raise ValueError('An APK changed after verification')
    arguments = shlex.join(['check', '--targets', str(targets), '--apk', str(apk), '--app-id', app_id])
    command = ['./gradlew', '--no-daemon']
    if offline:
        command.append('--offline')
    command.extend([':compat-checker:run', '--args=' + arguments])
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open('w') as output:
        subprocess.run(command, cwd=repository, stdout=output, stderr=subprocess.STDOUT, check=True, timeout=300)


def run_regression(app_id, reports, targets=None, cache=None):
    policy = get_policy(app_id)
    targets = targets or Path(policy.targets_path)
    cache = cache or Path('.cache') / f'{policy.app_id}-apks'
    reports.mkdir(parents=True, exist_ok=True)
    summary = []
    try:
        profiles = registered_window(json.loads(targets.read_text()), policy.app_id)
    except (OSError, ValueError) as error:
        summary.append({'appId': policy.app_id, 'status': 'FAILED', 'error': str(error)})
        (reports / 'results.json').write_text(json.dumps(summary, indent=2) + '\n')
        return summary
    for profile in profiles:
        identity = profile['identity']
        key = f"{policy.app_id}-{identity['packageName']}-{identity['abi']}-{identity['versionCode']}-{identity['apkSha256']}"
        try:
            apk = ensure_apk(profile, cache, policy.app_id)
            check_apk(Path.cwd(), targets, apk, identity['apkSha256'], reports / (key + '.log'), app_id=policy.app_id)
            summary.append({'appId': policy.app_id, 'identity': identity, 'status': 'PASSED'})
        except (OSError, ValueError, HTTPException, subprocess.SubprocessError) as error:
            summary.append({'appId': policy.app_id, 'identity': identity, 'status': 'FAILED', 'error': str(error)})
    (reports / 'results.json').write_text(json.dumps(summary, indent=2) + '\n')
    return summary


def main():
    parser = argparse.ArgumentParser()
    apps = parser.add_mutually_exclusive_group(required=True)
    apps.add_argument('--app-id')
    apps.add_argument('--all-apps', action='store_true')
    parser.add_argument('--targets', type=Path)
    parser.add_argument('--cache', type=Path)
    parser.add_argument('--reports', type=Path, default=Path('work/static-regression'))
    args = parser.parse_args()
    if args.all_apps and (args.targets is not None or args.cache is not None):
        parser.error('--all-apps uses each application policy and its own APK cache')
    app_ids = ['wechat', 'qq', 'wecom'] if args.all_apps else [args.app_id]
    summary = []
    for app_id in app_ids:
        reports = args.reports / app_id if args.all_apps else args.reports
        summary.extend(run_regression(app_id, reports, args.targets, args.cache))
    args.reports.mkdir(parents=True, exist_ok=True)
    (args.reports / 'results.json').write_text(json.dumps(summary, indent=2) + '\n')
    if any(row['status'] != 'PASSED' for row in summary):
        raise SystemExit('Static regression failed; see per-build logs and results.json')


if __name__ == '__main__':
    main()
