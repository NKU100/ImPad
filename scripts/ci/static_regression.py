"""Run the shared RULES checker against fixed acceptance representatives."""
import argparse
from http.client import HTTPException
import json
import re
import shlex
import subprocess
import shutil
from pathlib import Path

from scripts.ci.acceptance import read_acceptance_snapshot, select_representatives
from scripts.ci.app_policy import get_policy
from scripts.ci.discover_latest_wechat import validate_official_apk_url
from scripts.ci.fetch_latest_wechat_apk import download_candidate, file_sha256, remote_metadata
from scripts.ci.download_url import validate_download_url
from scripts.ci.apk_inputs import unpack_apk_inputs


def record_apk_inputs(record, cache, base):
    files = record.get('source', {}).get('apkFiles', [])
    paths = [Path(base)] + [Path(cache) / (item['sha256'].lower() + '.apk') for item in files[1:]]
    for item, path in zip(files, paths):
        if not path.is_file() or file_sha256(path) != item['sha256'].lower():
            raise ValueError('Fixed base/split APK hash differs from the acceptance record')
    return paths


def ensure_apk(record, cache, app_id):
    policy = get_policy(app_id)
    identity = record.get('identity', {})
    if identity.get('packageName') != policy.package_name:
        raise ValueError('Acceptance record package does not match app policy')
    source = record.get('source', {})
    source_url = source.get('url')
    external = source.get('type') == 'signed-download'
    if external:
        validate_download_url(source_url)
    else:
        validate_official_apk_url(source_url, policy.official_apk_prefix)
    digest = identity.get('apkSha256', '').lower()
    if not re.fullmatch(r'[a-f0-9]{64}', digest):
        raise ValueError('Acceptance record APK SHA-256 is invalid')
    path = cache / (digest + '.apk')
    try:
        record_apk_inputs(record, cache, path)
        cached = path.is_file() and file_sha256(path) == digest
    except ValueError:
        cached = False
    if not cached:
        expected_archive = source.get('archiveSha256', digest)
        options = {'allow_external': True} if external else {}
        archive, archive_hash, _, _ = download_candidate(
            source_url, cache, expected_archive, remote_metadata(source_url, policy, **options), policy, **options)
        if archive_hash != expected_archive or file_sha256(archive) != expected_archive:
            raise ValueError('Fixed source archive hash differs from the acceptance record')
        inputs = unpack_apk_inputs(archive, cache / ('unpacked-' + archive_hash), policy.package_name)
        expected = [item['sha256'].lower() for item in source.get('apkFiles', [])] or [digest]
        if [file_sha256(item) for item in inputs] != expected:
            raise ValueError('Fixed base/split APK hashes differ from the acceptance record')
        for item, item_hash in zip(inputs, expected):
            destination = cache / (item_hash + '.apk')
            if item.resolve() != destination.resolve():
                shutil.copyfile(item, destination)
    if file_sha256(path) != digest:
        raise ValueError('Regression APK hash does not match the fixed acceptance record')
    return path


def check_apk(repository, apk, digest, log, offline=False, app_id='wechat', abi_splits=()):
    apk = apk.resolve()
    if file_sha256(apk) != digest.lower():
        raise ValueError('An APK changed after verification')
    report = log.with_suffix('.json')
    inputs = ['check-rules', '--app-id', app_id, '--apk', str(apk), '--report', str(report)]
    if abi_splits:
        inputs += ['--abi-splits', ','.join(str(Path(path).resolve()) for path in abi_splits)]
    arguments = shlex.join(inputs)
    command = ['./gradlew', '--no-daemon']
    if offline:
        command.append('--offline')
    command.extend([':compat-checker:run', '--args=' + arguments])
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open('w') as output:
        subprocess.run(command, cwd=repository, stdout=output, stderr=subprocess.STDOUT,
                       check=True, timeout=300)
    result = json.loads(report.read_text())
    if result.get('selection', {}).get('status') != 'SELECTED':
        raise ValueError('Rule checker did not select one complete plan')
    return result


def run_regression(app_id, reports, cache=None, repository=None):
    policy = get_policy(app_id)
    repository = Path(repository or Path.cwd()).resolve()
    acceptance_path = repository / f'compatibility/{policy.app_id}/acceptance.json'
    cache = Path(cache or Path('.cache') / f'{policy.app_id}-apks')
    reports.mkdir(parents=True, exist_ok=True)
    summary = []
    snapshot = None
    try:
        snapshot = read_acceptance_snapshot(acceptance_path, policy.app_id, False,
                                            repository=repository)
        records = select_representatives(snapshot.records)
        if not records:
            raise ValueError('Fixed acceptance regression requires at least one representative')
    except (OSError, ValueError) as error:
        summary.append({'appId': policy.app_id, 'status': 'FAILED', 'error': str(error)})
        (reports / 'results.json').write_text(json.dumps(summary, indent=2) + '\n')
        return summary

    for record in records:
        identity = record['identity']
        key = (f"{policy.app_id}-{identity['packageName']}-{identity['abi']}-"
               f"{identity['versionCode']}-{identity['apkSha256']}")
        try:
            apk = ensure_apk(record, cache, policy.app_id)
            inputs = record_apk_inputs(record, cache, apk)
            log = reports / (key + '.log')
            options = {'abi_splits': inputs[1:]} if len(inputs) > 1 else {}
            checked = check_apk(repository, apk, identity['apkSha256'], log,
                                app_id=policy.app_id, **options)
            if checked.get('identity') != identity:
                raise ValueError('Regression checker identity differs from the fixed acceptance record')
            expected = record.get('expectedPlan', {})
            actual = checked.get('selection', {})
            if (actual.get('selectedRuleId') != expected.get('ruleId')
                    or actual.get('strategyId') != expected.get('strategyId')
                    or actual.get('targets') != expected.get('targets')):
                raise ValueError('Regression selection differs from its fixed acceptance record')
            report_path = log.with_suffix('.json')
            summary.append({
                'appId': policy.app_id,
                'recordId': record['recordId'],
                'identity': identity,
                'expectedPlan': expected,
                'acceptanceSnapshotSha256': snapshot.sha256,
                'initialMigration': False,
                'staticEvidence': {'path': report_path.name, 'sha256': file_sha256(report_path)},
                'status': 'PASSED',
            })
        except (OSError, ValueError, HTTPException, subprocess.SubprocessError) as error:
            summary.append({
                'appId': policy.app_id,
                'recordId': record.get('recordId'),
                'identity': identity,
                'expectedPlan': record.get('expectedPlan'),
                'acceptanceSnapshotSha256': snapshot.sha256,
                'initialMigration': False,
                'status': 'FAILED',
                'error': str(error),
            })
    (reports / 'results.json').write_text(json.dumps(summary, indent=2) + '\n')
    return summary


def main():
    parser = argparse.ArgumentParser()
    apps = parser.add_mutually_exclusive_group(required=True)
    apps.add_argument('--app-id')
    apps.add_argument('--all-apps', action='store_true')
    parser.add_argument('--cache', type=Path)
    parser.add_argument('--reports', type=Path, default=Path('work/static-regression'))
    args = parser.parse_args()
    if args.all_apps and args.cache is not None:
        parser.error('--all-apps uses each application policy and its own APK cache')
    app_ids = ['wechat', 'qq', 'wecom'] if args.all_apps else [args.app_id]
    summary = []
    for app_id in app_ids:
        reports = args.reports / app_id if args.all_apps else args.reports
        summary.extend(run_regression(app_id, reports, args.cache))
    args.reports.mkdir(parents=True, exist_ok=True)
    (args.reports / 'results.json').write_text(json.dumps(summary, indent=2) + '\n')
    if any(row['status'] != 'PASSED' for row in summary):
        raise SystemExit('Static regression failed; see per-build logs and results.json')


if __name__ == '__main__':
    main()
