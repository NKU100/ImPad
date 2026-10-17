"""Prepare an official APK for latest or registered runtime validation."""
import argparse
import copy
from http.client import HTTPException
import importlib
import json
import re
import shutil
from pathlib import Path

from scripts.ci.app_policy import get_policy, validate_targets
from scripts.ci.discover_latest_wechat import discover_from_html, fetch_official_page, validate_official_apk_url
from scripts.ci.fetch_latest_wechat_apk import download_candidate, remote_metadata
from scripts.ci.select_compatibility_window import SUPPORTED_STATUSES


def replay_report(targets, url, digest, app_id="wechat", selection_mode="latest"):
    policy = get_policy(app_id)
    validate_targets(targets, policy.app_id)
    validate_official_apk_url(url, policy.official_apk_prefix)
    matches = [row for row in targets if row['identity']['packageName'] == policy.package_name
               and row['identity']['abi'] in policy.supported_abis and row['sourceUrl'] == url
               and row['identity']['apkSha256'].lower() == digest.lower()]
    if len(matches) != 1:
        raise ValueError('Selected exact APK is not registered; run compatibility adaptation first')
    profile = copy.deepcopy(matches[0])
    allowed = SUPPORTED_STATUSES | ({'static-verified'} if app_id == 'qq' and selection_mode == 'baseline' else set())
    if profile['verificationStatus'] not in allowed:
        raise ValueError('Selected profile is not runtime-verified; run compatibility adaptation first')
    profile['verificationStatus'] = 'static-verified'
    return {
        'appId': policy.app_id,
        'status': 'STATIC_VERIFIED_PENDING_RUNTIME',
        'identity': profile['identity'],
        'sourceUrl': url,
        'suggestedProfile': profile,
        'checkedVersions': [profile['identity']['versionName']],
        'replay': True,
        'selectionMode': selection_mode,
    }


def write_local_runtime_assets(profile, app_id, directory):
    policy = get_policy(app_id)
    if profile.get('identity', {}).get('packageName') != policy.package_name:
        raise ValueError('Runtime profile package does not match the registered application policy')
    local_profile = copy.deepcopy(profile)
    local_profile['sourceUrl'] = None
    target_path = Path(directory) / 'assets/compatibility-local' / policy.app_id / 'targets.json'
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(json.dumps([local_profile], indent=2) + '\n')
    scope_path = Path(directory) / 'resources/META-INF/xposed/scope.list'
    scope_path.parent.mkdir(parents=True, exist_ok=True)
    scope_path.write_text(policy.package_name + '\n')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--app-id', default='wechat')
    parser.add_argument('--targets', type=Path)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--mode', choices=('latest', 'registered', 'baseline'), default='latest')
    parser.add_argument('--apk-sha256', help='Select a registered APK variant by SHA-256')
    args = parser.parse_args()
    if args.apk_sha256 and not re.fullmatch(r'[a-fA-F0-9]{64}', args.apk_sha256):
        raise ValueError('--apk-sha256 must contain 64 hexadecimal characters')
    policy = get_policy(args.app_id)
    targets_path = args.targets or Path(policy.targets_path)
    targets = json.loads(targets_path.read_text())
    validate_targets(targets, policy.app_id)
    if args.mode == 'baseline' and policy.app_id != 'qq':
        raise ValueError('Initial static baseline replay is available only for QQ')
    if args.mode in ('registered', 'baseline'):
        allowed = SUPPORTED_STATUSES | ({'static-verified'} if args.mode == 'baseline' else set())
        matches = [row for row in targets if row['identity']['packageName'] == policy.package_name
                   and row['identity']['abi'] in policy.supported_abis
                   and row['verificationStatus'] in allowed]
        if not matches:
            raise ValueError('No runtime-verified registered APK is available for this application policy')
        highest_version_code = max(row['identity']['versionCode'] for row in matches)
        matches = [row for row in matches if row['identity']['versionCode'] == highest_version_code]
        if args.apk_sha256:
            matches = [row for row in matches
                       if row['identity']['apkSha256'].lower() == args.apk_sha256.lower()]
        if len(matches) != 1:
            raise ValueError('Highest runtime-verified APK selection is ambiguous; specify --apk-sha256')
        selected = matches[0]
        url = selected['sourceUrl']
        expected_sha256 = selected['identity']['apkSha256'].lower()
    else:
        strategy = importlib.import_module(policy.discovery_strategy)
        candidate = strategy.discover(policy)
        url = candidate.url
        validate_official_apk_url(url, policy.official_apk_prefix)
        matches = [row for row in targets if row['sourceUrl'] == url]
        digests = {row['identity']['apkSha256'].lower() for row in matches}
        expected_sha256 = next(iter(digests)) if len(digests) == 1 else ''
    validate_official_apk_url(url, policy.official_apk_prefix)
    args.directory.mkdir(parents=True, exist_ok=True)
    try:
        metadata = remote_metadata(url, policy)
    except (HTTPException, OSError):
        if args.mode not in ('registered', 'baseline'):
            raise
        # Registered targets pin the APK digest; HEAD metadata only enables cache reuse.
        metadata = {'content_length': None, 'last_modified': '', 'etag': ''}
    apk, digest, _, _ = download_candidate(url, args.directory / 'cache', expected_sha256,
                                           metadata, policy)
    if args.mode == 'latest':
        matches = [row for row in targets if row['sourceUrl'] == url
                   and row['identity']['apkSha256'].lower() == digest.lower()]
        if len(matches) != 1 or matches[0]['verificationStatus'] not in SUPPORTED_STATUSES:
            raise ValueError('Latest official APK is not runtime-verified; run compatibility adaptation first')
        selected = matches[0]
    report = replay_report(targets, url, digest, policy.app_id, args.mode)
    if policy.app_id == 'qq':
        from scripts.ci.apps.qq.runtime_probe import manifest_expectation
        report['requestIdentityExpectation'] = manifest_expectation(apk, digest)
    shutil.copy2(apk, args.directory / f'{policy.app_id}-{digest}.apk')
    (args.directory / 'candidate-report.json').write_text(json.dumps(report, indent=2) + '\n')
    write_local_runtime_assets(report['suggestedProfile'], policy.app_id, args.directory)


if __name__ == '__main__':
    main()
