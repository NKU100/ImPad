"""Run the shared RULES checker against selected acceptance representatives."""
import argparse
from http.client import HTTPException
import json
import re
import shlex
import subprocess
import shutil
from pathlib import Path
from urllib.error import HTTPError, URLError

from scripts.ci.app_policy import POLICIES, get_policy
from scripts.ci.discover_latest_wechat import validate_official_apk_url
from scripts.ci.fetch_latest_wechat_apk import (ApkDigestMismatch, download_candidate,
                                                file_sha256, remote_metadata, TruncatedDownload)
from scripts.ci.download_url import validate_download_url
from scripts.ci.apk_inputs import unpack_apk_inputs
from scripts.ci.semantic_protocol import validate_check_report


class SemanticRegressionMismatch(ValueError):
    """A fresh valid checker report proves the frozen semantic result changed."""


class StaticCheckerExecutionError(subprocess.SubprocessError):
    """The checker did not produce a fresh valid report for its exact inputs."""


class HistoricalApkUnavailable(ValueError):
    """A pinned historical APK could not be acquired from its source."""


def is_unavailable_regression(row):
    return (isinstance(row, dict) and isinstance(row.get('appId'), str)
            and row['appId'] in POLICIES
            and row.get('status') == 'SKIPPED' and row.get('skipReason') == 'APK_UNAVAILABLE'
            and isinstance(row.get('recordId'), str) and bool(row['recordId'])
            and isinstance(row.get('error'), str) and bool(row['error'])
            and 'staticEvidence' not in row and 'failureKind' not in row)


def regression_status(rows):
    if not isinstance(rows, list) or not rows or not all(isinstance(row, dict) and (
            row.get('status') == 'PASSED' or is_unavailable_regression(row)) for row in rows):
        return 'FAILED'
    return 'PARTIAL' if any(is_unavailable_regression(row) for row in rows) else 'PASSED'


def record_apk_inputs(record, cache, base):
    files = record.get('source', {}).get('apkFiles', [])
    paths = [Path(base)] + [Path(cache) / (item['sha256'].lower() + '.apk') for item in files[1:]]
    for item, path in zip(files, paths):
        if not path.is_file() or file_sha256(path) != item['sha256'].lower():
            raise ValueError('Fixed base/split APK hash differs from the acceptance record')
    return paths


def _validate_record_package(record, policy):
    identity = record.get('identity', {})
    if identity.get('packageName') != policy.package_name:
        raise ValueError('Acceptance record package does not match app policy')


def _record_apk_digest(record, policy):
    _validate_record_package(record, policy)
    identity = record.get('identity', {})
    digest = identity.get('apkSha256', '').lower()
    if not re.fullmatch(r'[a-f0-9]{64}', digest):
        raise ValueError('Acceptance record APK SHA-256 is invalid')
    return digest


def _cached_apk_path(record, cache, digest):
    path = Path(cache) / (digest + '.apk')
    try:
        record_apk_inputs(record, cache, path)
        cached = path.is_file() and file_sha256(path) == digest
    except ValueError:
        cached = False
    return path if cached else None


def cached_historical_apk(record, cache, app_id):
    """Return a fully verified cached historical APK without accessing its source URL."""
    policy = get_policy(app_id)
    digest = _record_apk_digest(record, policy)
    return _cached_apk_path(record, cache, digest)


def ensure_apk(record, cache, app_id):
    policy = get_policy(app_id)
    _validate_record_package(record, policy)
    source = record.get('source', {})
    source_url = source.get('url')
    external = source.get('type') == 'signed-download'
    if external:
        validate_download_url(source_url)
    else:
        validate_official_apk_url(source_url, policy.official_apk_prefix)
    digest = _record_apk_digest(record, policy)
    path = Path(cache) / (digest + '.apk')
    if _cached_apk_path(record, cache, digest) is None:
        expected_archive = source.get('archiveSha256', digest)
        options = {'allow_external': True} if external else {}
        try:
            remote = remote_metadata(source_url, policy, **options)
            archive, archive_hash, _, _ = download_candidate(
                source_url, cache, expected_archive, remote, policy, **options)
        except (ApkDigestMismatch, URLError, TimeoutError, ConnectionError, HTTPException) as error:
            if isinstance(error, HTTPError):
                detail = f'HTTP {error.code}'
            elif isinstance(error, (ApkDigestMismatch, TruncatedDownload)):
                detail = str(error)
            else:
                detail = type(error).__name__
            raise HistoricalApkUnavailable(detail) from error
        if archive_hash != expected_archive or file_sha256(archive) != expected_archive:
            raise HistoricalApkUnavailable('Downloaded archive hash differs from the acceptance record')
        inputs = unpack_apk_inputs(archive, cache / ('unpacked-' + archive_hash), policy.package_name)
        expected = [item['sha256'].lower() for item in source.get('apkFiles', [])] or [digest]
        if [file_sha256(item) for item in inputs] != expected:
            raise HistoricalApkUnavailable('Downloaded base/split APK hashes differ from the acceptance record')
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
        raise StaticCheckerExecutionError('An APK changed after verification')
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
    report.unlink(missing_ok=True)
    expected_hashes = [digest.lower(), *(file_sha256(Path(path)).lower() for path in abi_splits)]
    with log.open('w') as output:
        completed = subprocess.run(command, cwd=repository, stdout=output, stderr=subprocess.STDOUT,
                                   check=False, timeout=300)
    try:
        result = json.loads(report.read_text())
        validate_check_report(result, expected_app_id=app_id)
        actual_hashes = [row['sha256'].lower() for row in result['inputHashes']]
        if actual_hashes != expected_hashes:
            raise ValueError('Checker report inputs differ from the exact base/split APK bytes')
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
        if completed.returncode != 0:
            raise subprocess.CalledProcessError(completed.returncode, command) from None
        raise StaticCheckerExecutionError('Rule checker did not produce a fresh valid report') from error
    if result['selection']['status'] in {'EVALUATION_ERROR', 'UNSUPPORTED_INPUT', 'INCOMPLETE'}:
        raise StaticCheckerExecutionError(
            f"Rule checker could not complete evaluation: {result['selection']['status']}")
    if result['selection']['status'] != 'SELECTED':
        raise SemanticRegressionMismatch(
            f"Rule checker returned {result['selection']['status']} for the frozen APK inputs")
    if completed.returncode != 0:
        raise subprocess.CalledProcessError(completed.returncode, command)
    return result


def classify_regression_failure(stage, error):
    if isinstance(error, SemanticRegressionMismatch):
        return 'check_failure'
    if isinstance(error, OSError) or stage in {'download', 'checker_execution'}:
        return 'execution_failure'
    if stage in {'checker_report', 'comparison', 'baseline'}:
        return 'check_failure'
    return 'execution_failure'


def run_grouped_cli(app_ids, reports, cache):
    from scripts.ci.grouped_static_regression import (REPORT_SCHEMA_VERSION,
        badge_regression_rows, group_report, run_grouped_regression)
    from scripts.ci.regression_groups import GROUPING_VERSION
    from scripts.ci.semantic_regression_baseline import load_verified_regression_pool

    repository = Path.cwd()
    applications = []
    for app_id in app_ids:
        destination = reports / app_id if len(app_ids) > 1 else reports
        try:
            baseline = load_verified_regression_pool(repository, app_id)
            result = run_grouped_regression(baseline, destination,
                                            cache or Path('.cache') / f'{app_id}-apks', repository)
        except (OSError, ValueError) as error:
            result = group_report(app_id, None, {'version': GROUPING_VERSION, 'excludedGroups': []}, [{
                'groupId': 'baseline', 'status': 'FAILED', 'failureKind': classify_regression_failure('baseline', error),
                'error': str(error),
            }])
            destination.mkdir(parents=True, exist_ok=True)
            (destination / 'results.json').write_text(json.dumps(result, indent=2) + '\n')
        applications.append(result)
    output = applications[0] if len(app_ids) == 1 else {
        'schemaVersion': REPORT_SCHEMA_VERSION, 'applications': applications,
    }
    reports.mkdir(parents=True, exist_ok=True)
    (reports / 'results.json').write_text(json.dumps(output, indent=2) + '\n')
    return regression_status(badge_regression_rows(output))


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
    if run_grouped_cli(app_ids, args.reports, args.cache) == 'FAILED':
        raise SystemExit('Static regression failed; see results.json')


if __name__ == '__main__':
    main()
