"""Classify and publish bounded compatibility workflow badge evidence."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import time


REPOSITORY = 'NKU100/ImPad'
WORKFLOWS = {
    'wechat': '.github/workflows/compatibility-wechat.yml',
    'qq': '.github/workflows/compatibility-qq.yml',
    'wecom': '.github/workflows/compatibility-wecom.yml',
}
APP_LABELS = {'wechat': '微信适配', 'qq': 'QQ适配', 'wecom': '企微适配'}
BRANCH = 'compatibility-badge-data'
REPORT_LIMIT = 8192
EVENT_LIMIT = 1024 * 1024
SHA_PATTERN = re.compile(r'^[0-9a-f]{40}$')
APK_SHA_PATTERN = re.compile(r'^[0-9a-f]{64}$')
VERSION_PATTERN = re.compile(r'^\d+(?:\.\d+){1,4}$')
REPORT_FIELDS = {
    'schemaVersion', 'appId', 'runId', 'runNumber', 'runAttempt', 'sha',
    'candidateSha256', 'versionName', 'versionCode', 'outcome', 'operation',
    'candidateStatus', 'validationOutcome', 'adaptationAttempted',
    'registrationStatus', 'prReady', 'regressionOutcome', 'checkOutcome',
    'buildOutcome', 'runtimeEnvironment',
}
OUTCOMES = {
    'VERIFIED', 'RUNTIME_VERIFIED', 'RUNTIME_INCOMPATIBLE', 'REGRESSION_FAILED',
    'NO_MATCH', 'CONFLICT', 'ERROR', 'CANCELLED', 'PENDING_RUNTIME',
    'ADAPTATION_INCOMPLETE', 'CANDIDATE_REJECTED', 'UNAVAILABLE',
}


def validate_report(report):
    try:
        encoded = json.dumps(report, separators=(',', ':'), ensure_ascii=True).encode()
    except (TypeError, ValueError) as error:
        raise ValueError('Compatibility badge evidence is not JSON serializable') from error
    if len(encoded) > REPORT_LIMIT:
        raise ValueError('Compatibility badge evidence exceeds the size limit')
    if not isinstance(report, dict) or set(report) != REPORT_FIELDS:
        raise ValueError('Compatibility badge evidence fields do not match schema version 1')
    if type(report['schemaVersion']) is not int or report['schemaVersion'] != 1:
        raise ValueError('Unsupported compatibility badge evidence schema')
    if report['appId'] not in WORKFLOWS:
        raise ValueError('Unsupported compatibility badge application')
    for field in ('runId', 'runNumber', 'runAttempt'):
        if type(report[field]) is not int or report[field] <= 0:
            raise ValueError(f'Invalid {field}')
    if not isinstance(report['sha'], str) or not SHA_PATTERN.fullmatch(report['sha']):
        raise ValueError('Invalid source commit SHA')
    candidate_sha = report['candidateSha256']
    if candidate_sha is not None and (
            not isinstance(candidate_sha, str) or not APK_SHA_PATTERN.fullmatch(candidate_sha)):
        raise ValueError('Invalid exact candidate APK SHA-256')
    version = report['versionName']
    if version is not None and (not isinstance(version, str) or not VERSION_PATTERN.fullmatch(version)):
        raise ValueError('Invalid candidate version name')
    version_code = report['versionCode']
    if version_code is not None and (type(version_code) is not int or version_code <= 0):
        raise ValueError('Invalid candidate version code')
    if (version is None) != (version_code is None) or (version is None) != (candidate_sha is None):
        raise ValueError('Candidate version and APK identity must be present together')
    if report['outcome'] not in OUTCOMES:
        raise ValueError('Invalid compatibility outcome')
    if report['operation'] not in {'verify', 'adapt'}:
        raise ValueError('Invalid operation')
    if report['candidateStatus'] not in {'PREPARED', 'INCOMPATIBLE', 'UNAVAILABLE'}:
        raise ValueError('Invalid candidate status')
    if report['validationOutcome'] not in OUTCOMES | {''}:
        raise ValueError('Invalid validation outcome')
    if type(report['adaptationAttempted']) is not bool or type(report['prReady']) is not bool:
        raise ValueError('Invalid adaptation evidence')
    if report['registrationStatus'] not in {'', 'REGISTERED'}:
        raise ValueError('Invalid registration status')
    for field in ('regressionOutcome', 'checkOutcome', 'buildOutcome'):
        if report[field] not in {'PASSED', 'FAILED', 'UNKNOWN'}:
            raise ValueError(f'Invalid {field}')
    if report['runtimeEnvironment'] not in {'HEALTHY', 'UNKNOWN'}:
        raise ValueError('Invalid runtime environment evidence')
    return report


def is_trusted_run(run):
    app_id = run.get('appId')
    return (app_id in WORKFLOWS
            and run.get('repository') == REPOSITORY
            and run.get('headRepository') == REPOSITORY
            and run.get('workflowPath') == WORKFLOWS[app_id]
            and run.get('event') in {'schedule', 'workflow_dispatch'}
            and run.get('branch') == 'main'
            and run.get('status') == 'completed'
            and all(type(run.get(field)) is int and run[field] > 0
                    for field in ('runId', 'runNumber', 'runAttempt'))
            and isinstance(run.get('sha'), str) and bool(SHA_PATTERN.fullmatch(run['sha'])))


def create_report(run, state):
    if not isinstance(run, dict) or not isinstance(state, dict):
        raise ValueError('Compatibility workflow context must be structured JSON')
    exact_candidate = state.get('candidateSource') in {
        'verify', 'runtime', 'rules-analysis', 'adaptation'}
    if (state.get('pendingAdaptation') is True
            and state.get('candidateSource') not in {'runtime', 'adaptation'}):
        exact_candidate = False
    version = state.get('versionName') if exact_candidate else None
    version_code = state.get('versionCode') if exact_candidate else None
    candidate_sha = state.get('candidateSha256') if exact_candidate else None
    report = {
        'schemaVersion': 1,
        'appId': run.get('appId'),
        'runId': run.get('runId'),
        'runNumber': run.get('runNumber'),
        'runAttempt': run.get('runAttempt'),
        'sha': run.get('sha'),
        'candidateSha256': candidate_sha,
        'versionName': version,
        'versionCode': version_code,
        'outcome': state.get('outcome', 'ERROR'),
        'operation': state.get('operation'),
        'candidateStatus': state.get('candidateStatus', 'UNAVAILABLE'),
        'validationOutcome': state.get('validationOutcome', ''),
        'adaptationAttempted': state.get('adaptationAttempted', False),
        'registrationStatus': state.get('registrationStatus', ''),
        'prReady': state.get('prReady', False),
        'regressionOutcome': state.get('regressionOutcome', 'UNKNOWN'),
        'checkOutcome': state.get('checkOutcome', 'UNKNOWN'),
        'buildOutcome': state.get('buildOutcome', 'UNKNOWN'),
        'runtimeEnvironment': state.get('runtimeEnvironment', 'UNKNOWN'),
    }
    return validate_report(report)


def runtime_summary(candidate, result, regression):
    app_id = candidate.get('appId') if isinstance(candidate, dict) else None
    candidate_status = candidate.get('status') if isinstance(candidate, dict) else None
    if app_id not in WORKFLOWS or candidate_status not in {'PREPARED', 'INCOMPATIBLE'}:
        app_id = app_id if app_id in WORKFLOWS else ''
        candidate_status = 'UNAVAILABLE'
        candidate = {}
    selection = candidate.get('diagnostic', {}).get('selection') if isinstance(
        candidate.get('diagnostic'), dict) else None
    outcome = selection if candidate_status == 'INCOMPATIBLE' and selection in {'NO_MATCH', 'CONFLICT'} else 'ERROR'
    identity = candidate.get('identity') if candidate_status == 'INCOMPATIBLE' else None
    runtime_environment = 'UNKNOWN'
    build_outcome = 'UNKNOWN'
    if candidate_status == 'PREPARED' and isinstance(result, dict):
        identity = candidate.get('identity')
        reported_candidate = result.get('candidate')
        bound = (result.get('schemaVersion') == 2 and isinstance(identity, dict)
                 and isinstance(reported_candidate, dict)
                 and reported_candidate.get('appId') == app_id
                 and reported_candidate.get('identity') == identity)
        if bound and result.get('outcome') in OUTCOMES:
            outcome = result['outcome']
            if outcome in {'VERIFIED', 'RUNTIME_VERIFIED', 'RUNTIME_INCOMPATIBLE'}:
                build_outcome = 'PASSED'
            runtime = result.get('runtime')
            if (outcome in {'VERIFIED', 'RUNTIME_VERIFIED'} and isinstance(runtime, dict)
                    and runtime.get('status') == 'RUNTIME_SMOKE_VERIFIED'):
                runtime_environment = 'HEALTHY'
            if (outcome == 'RUNTIME_INCOMPATIBLE' and isinstance(runtime, dict)
                    and runtime.get('status') == 'RUNTIME_REJECTED'):
                runtime_environment = 'HEALTHY'
        else:
            identity = None
    elif candidate_status != 'INCOMPATIBLE':
        identity = None
    regression_outcome = classify_regression(regression)
    regression_check = classify_regression_check(regression)
    check_outcome = ('FAILED' if regression_check == 'FAILED'
                     else 'PASSED' if candidate_status == 'PREPARED'
                     or outcome in {'NO_MATCH', 'CONFLICT'} else 'UNKNOWN')
    identity = identity if isinstance(identity, dict) else {}
    version = identity.get('versionName')
    version_code = identity.get('versionCode')
    digest = identity.get('apkSha256')
    if (not isinstance(version, str) or not VERSION_PATTERN.fullmatch(version)
            or type(version_code) is not int or version_code <= 0
            or not isinstance(digest, str) or not APK_SHA_PATTERN.fullmatch(digest)):
        version = version_code = digest = None
        if candidate_status == 'PREPARED':
            candidate_status = 'UNAVAILABLE'
            outcome = 'ERROR'
    return {
        'appId': app_id, 'candidateStatus': candidate_status, 'outcome': outcome,
        'validationOutcome': (result.get('outcome', '') if isinstance(result, dict)
                              and result.get('outcome') in OUTCOMES else ''),
        'versionName': version, 'versionCode': version_code, 'candidateSha256': digest,
        'regressionOutcome': regression_outcome, 'checkOutcome': check_outcome,
        'buildOutcome': build_outcome, 'runtimeEnvironment': runtime_environment,
    }


def classify_regression(regression):
    rows = [regression] if isinstance(regression, dict) else regression
    if isinstance(rows, list) and rows and all(isinstance(row, dict) for row in rows):
        if any(row.get('status') == 'FAILED' and row.get('failureKind') == 'check_failure'
               and isinstance(row.get('recordId'), str) and row['recordId'] for row in rows):
            return 'FAILED'
        if all(row.get('status') == 'PASSED' for row in rows):
            return 'PASSED'
    return 'UNKNOWN'


def classify_regression_check(regression):
    rows = [regression] if isinstance(regression, dict) else regression
    if (isinstance(rows, list) and any(
            isinstance(row, dict) and row.get('status') == 'FAILED'
            and row.get('failureKind') == 'check_failure'
            and not (isinstance(row.get('recordId'), str) and row['recordId'])
            for row in rows)):
        return 'FAILED'
    return 'UNKNOWN'


def write_runtime_summary(candidate, result, regression, output_path, github_output=None):
    summary_data = runtime_summary(candidate, result, regression)
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(summary_data, separators=(',', ':')) + '\n')
    if github_output:
        mapping = {
            'badge_outcome': 'outcome', 'badge_candidate_status': 'candidateStatus',
            'badge_version_name': 'versionName', 'badge_version_code': 'versionCode',
            'badge_candidate_sha256': 'candidateSha256',
            'badge_regression_outcome': 'regressionOutcome',
            'badge_check_outcome': 'checkOutcome', 'badge_build_outcome': 'buildOutcome',
            'badge_runtime_environment': 'runtimeEnvironment',
        }
        with Path(github_output).open('a') as output:
            for key, field in mapping.items():
                output.write(f'{key}={summary_data[field] if summary_data[field] is not None else ""}\n')
    return summary_data


def write_adaptation_summary(task_directory, claimed, model_outcome, needs_model,
                             static_validation_outcome, github_output, app_id):
    task = Path(task_directory)
    state_path = task / 'state.json'
    state = _read_json(state_path, limit=EVENT_LIMIT) if state_path.is_file() else None
    source = state.get('report') if isinstance(state, dict) else None
    task_bound = (isinstance(state, dict) and state.get('appId') == app_id
                  and isinstance(source, dict) and source.get('appId') == app_id)
    if not task_bound:
        source = None
    identity = source.get('identity') if isinstance(source, dict) else None
    worktree = task / 'missing-worktree'
    if task_bound and isinstance(state.get('worktree'), str):
        try:
            worktree = Path(state['worktree']).resolve()
            worktree.relative_to(task.resolve())
        except (OSError, ValueError):
            worktree = task / 'missing-worktree'
    prepared_path = worktree / 'work/analysis/candidate-report.json'
    prepared = _read_json(prepared_path, limit=EVENT_LIMIT) if prepared_path.is_file() else None
    same_identity = (isinstance(identity, dict) and isinstance(prepared, dict)
                     and prepared.get('status') == 'PREPARED'
                     and prepared.get('appId') == app_id
                     and prepared.get('identity') == identity)
    static_verified = (task_bound and claimed == 'true' and static_validation_outcome == 'success'
                       and same_identity)
    regression_path = worktree / 'work/runtime-smoke/regression/results.json'
    regression = (_read_json(regression_path, limit=1024 * 1024)
                  if regression_path.is_file() else None)
    identity = identity if isinstance(identity, dict) else {}
    version = identity.get('versionName')
    version_code = identity.get('versionCode')
    digest = identity.get('apkSha256')
    if (not isinstance(version, str) or not VERSION_PATTERN.fullmatch(version)
            or type(version_code) is not int or version_code <= 0
            or not isinstance(digest, str) or not APK_SHA_PATTERN.fullmatch(digest)):
        version = version_code = digest = None
    attempted = (task_bound and claimed == 'true' and (
        model_outcome in {'success', 'failure'} or needs_model == 'false'))
    evidence = {
        'attempted': attempted,
        'static_verified': static_verified,
        'candidate_status': 'PREPARED' if static_verified else (
            'INCOMPATIBLE' if version is not None else 'UNAVAILABLE'),
        'version_name': version,
        'version_code': version_code,
        'candidate_sha256': digest,
        'regression_outcome': ('PASSED' if static_verified else classify_regression(regression)),
        'check_outcome': ('PASSED' if static_verified else classify_regression_check(regression)),
        'build_outcome': 'PASSED' if static_verified else 'UNKNOWN',
    }
    with Path(github_output).open('a') as output:
        for key, value in evidence.items():
            output.write(f'{key}={str(value).lower() if isinstance(value, bool) else value or ""}\n')
    return evidence


def _badge(state, message, color, version=None, app_id=None):
    return {'state': state, 'label': APP_LABELS.get(app_id, '适配'),
            'message': message, 'color': color,
            'version': f'{version} · {message}' if version else message}


def classify(run, report):
    if run is None:
        return _badge('gray', '暂无记录', 'lightgrey')
    app_id = run.get('appId')
    if run.get('conclusion') == 'cancelled':
        return _badge('gray', '已取消', 'lightgrey', app_id=app_id)
    if run.get('conclusion') == 'timed_out':
        return _badge('gray', '执行异常', 'lightgrey', app_id=app_id)
    try:
        validate_report(report)
    except (TypeError, ValueError):
        report = None
    if (report is None or not is_trusted_run(run)
            or any(report[field] != run.get(field) for field in
                   ('appId', 'runId', 'runNumber', 'runAttempt', 'sha'))):
        return _badge('gray', '执行异常', 'lightgrey', app_id=app_id)

    version = report['versionName']
    if report['regressionOutcome'] == 'FAILED':
        return _badge('red', '回归失败', 'red', version, app_id)
    if report['outcome'] == 'RUNTIME_INCOMPATIBLE':
        if report['runtimeEnvironment'] == 'HEALTHY':
            return _badge('red', '验证未通过', 'red', version, app_id)
        return _badge('gray', '执行异常', 'lightgrey', version, app_id)
    if report['checkOutcome'] == 'FAILED':
        return _badge('red', '检查失败', 'red', version, app_id)
    if report['buildOutcome'] == 'FAILED':
        return _badge('red', '构建失败', 'red', version, app_id)

    if run.get('conclusion') != 'success':
        if (report['operation'] == 'verify'
                and report['outcome'] in {'NO_MATCH', 'CONFLICT'}):
            return _badge('orange', '需适配', 'orange', version, app_id)
        if report['outcome'] == 'ADAPTATION_INCOMPLETE' and report['adaptationAttempted']:
            return _badge('orange', '适配未完成', 'orange', version, app_id)
        return _badge('gray', '执行异常', 'lightgrey', version, app_id)

    complete_static = (report['checkOutcome'] == 'PASSED'
                       and report['buildOutcome'] == 'PASSED'
                       and report['regressionOutcome'] == 'PASSED')
    has_exact_candidate = (report['versionName'] is not None
                          and report['versionCode'] is not None
                          and report['candidateSha256'] is not None)
    if (has_exact_candidate and not report['adaptationAttempted']
            and report['candidateStatus'] == 'PREPARED'
            and report['validationOutcome'] in {'VERIFIED', 'RUNTIME_VERIFIED'}
            and report['outcome'] in {'VERIFIED', 'RUNTIME_VERIFIED'}
            and complete_static):
        return _badge('green', '已兼容', 'brightgreen', version, app_id)

    if (report['operation'] == 'verify' and report['candidateStatus'] == 'INCOMPATIBLE'
            and report['outcome'] in {'NO_MATCH', 'CONFLICT'}):
        return _badge('orange', '需适配', 'orange', version, app_id)

    if report['adaptationAttempted']:
        if (has_exact_candidate and report['outcome'] in {'VERIFIED', 'RUNTIME_VERIFIED'}
                and report['validationOutcome'] in {'VERIFIED', 'RUNTIME_VERIFIED'}
                and complete_static and report['registrationStatus'] == 'REGISTERED'
                and report['prReady']):
            return _badge('yellow', '待合并', 'yellow', version, app_id)
        if (has_exact_candidate and report['outcome'] == 'PENDING_RUNTIME'
                and report['validationOutcome'] == 'PENDING_RUNTIME'
                and report['checkOutcome'] == 'PASSED'
                and report['regressionOutcome'] == 'PASSED'):
            return _badge('yellow', '待验证', 'yellow', version, app_id)
        if report['outcome'] == 'ADAPTATION_INCOMPLETE':
            return _badge('orange', '适配未完成', 'orange', version, app_id)

    return _badge('gray', '执行异常', 'lightgrey', version, app_id)


def should_replace(current, incoming):
    if current is None:
        return True
    if not isinstance(current, dict) or not isinstance(incoming, dict):
        return False
    old = (current.get('runNumber', 0), current.get('runAttempt', 0))
    new = (incoming.get('runNumber', 0), incoming.get('runAttempt', 0))
    return new > old


def validate_record(record, app_id=None):
    fields = {'schemaVersion', 'state', 'label', 'message', 'color', 'version',
              'versionName', 'versionCode', 'candidateSha256', 'appId', 'runNumber',
              'runAttempt', 'runId', 'sha'}
    if not isinstance(record, dict) or set(record) != fields:
        raise ValueError('Published compatibility badge record fields do not match schema')
    if (type(record['schemaVersion']) is not int or record['schemaVersion'] != 1
            or record['appId'] not in WORKFLOWS
            or (app_id is not None and record['appId'] != app_id)):
        raise ValueError('Invalid published compatibility badge identity')
    allowed = {
        'green': {('已兼容', 'brightgreen')},
        'yellow': {('待合并', 'yellow'), ('待验证', 'yellow')},
        'orange': {('需适配', 'orange'), ('适配未完成', 'orange')},
        'red': {('回归失败', 'red'), ('验证未通过', 'red'),
                ('检查失败', 'red'), ('构建失败', 'red')},
        'gray': {('执行异常', 'lightgrey'), ('已取消', 'lightgrey'), ('暂无记录', 'lightgrey')},
    }
    state = record['state']
    label, color = record['label'], record['color']
    version = record['version']
    version_name, version_code, candidate_sha = (record['versionName'], record['versionCode'],
                                                  record['candidateSha256'])
    if (state not in allowed or label != APP_LABELS[record['appId']]
            or not isinstance(version, str)):
        raise ValueError('Invalid published compatibility badge appearance')
    message = record['message']
    if not isinstance(message, str):
        raise ValueError('Invalid published compatibility badge message')
    valid = False
    for status, expected_color in allowed[state]:
        if color != expected_color:
            continue
        if message == status and version == status:
            valid = True
        elif message == version and message.endswith(f' · {status}'):
            version_prefix = message.removesuffix(f' · {status}')
            valid = bool(VERSION_PATTERN.fullmatch(version_prefix)) and version_name == version_prefix
    if not valid:
        raise ValueError('Published compatibility badge state and appearance disagree')
    if version_name is None:
        if version_code is not None or candidate_sha is not None:
            raise ValueError('Candidate metadata must be present together')
    elif (not isinstance(version_name, str) or not VERSION_PATTERN.fullmatch(version_name)
          or type(version_code) is not int or version_code <= 0
          or not isinstance(candidate_sha, str) or not APK_SHA_PATTERN.fullmatch(candidate_sha)):
        raise ValueError('Invalid published candidate identity')
    for field in ('runNumber', 'runAttempt', 'runId'):
        if type(record[field]) is not int or record[field] <= 0:
            raise ValueError(f'Invalid {field}')
    if not isinstance(record['sha'], str) or not SHA_PATTERN.fullmatch(record['sha']):
        raise ValueError('Invalid published source commit SHA')
    return record


def _read_json(path, limit=REPORT_LIMIT):
    try:
        source = Path(path)
        if source.is_symlink():
            return None
        with source.open('rb') as stream:
            raw = stream.read(limit + 1)
        if len(raw) > limit:
            return None
        return json.loads(raw)
    except (OSError, ValueError, TypeError):
        return None


def _event_run(event):
    run = event['workflow_run']
    head = run.get('head_repository') or {}
    return {
        'repository': event['repository']['full_name'],
        'headRepository': head.get('full_name'), 'workflowPath': run.get('path'),
        'event': run.get('event'), 'branch': run.get('head_branch'),
        'status': run.get('status'), 'conclusion': run.get('conclusion'),
        'runId': run.get('id'), 'runNumber': run.get('run_number'),
        'runAttempt': run.get('run_attempt'), 'sha': run.get('head_sha'),
        'appId': next((app for app, path in WORKFLOWS.items() if path == run.get('path')), None),
    }


def _git(*args, input_text=None):
    return subprocess.run(['git', *args], check=True, text=True, input=input_text,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.strip()


def _remote_current(app_id):
    ref = f'refs/remotes/origin/{BRANCH}'
    fetched = subprocess.run(['git', 'fetch', '--no-tags', 'origin', f'refs/heads/{BRANCH}:{ref}'],
                             text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if fetched.returncode:
        return None, None
    commit = _git('rev-parse', ref)
    path = f'{app_id}.json'
    raw = subprocess.run(['git', 'show', f'{ref}:{path}'], text=True,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if raw.returncode or len(raw.stdout.encode()) > REPORT_LIMIT:
        return commit, None
    try:
        current = json.loads(raw.stdout)
        validate_record(current, app_id)
        return commit, current
    except ValueError:
        return commit, None


def publish(record, app_id, attempts=5):
    validate_record(record, app_id)
    payload = json.dumps(record, separators=(',', ':'), ensure_ascii=False) + '\n'
    if len(payload.encode()) > REPORT_LIMIT:
        raise ValueError('Published compatibility badge record exceeds the size limit')
    for _ in range(attempts):
        previous, current = _remote_current(app_id)
        if current is not None and not should_replace(current, record):
            return False
        blob = _git('hash-object', '-w', '--stdin', input_text=payload)
        index = os.environ.get('GIT_INDEX_FILE')
        temporary_index = str(Path('work/compatibility-badge/index'))
        Path(temporary_index).parent.mkdir(parents=True, exist_ok=True)
        os.environ['GIT_INDEX_FILE'] = temporary_index
        try:
            if previous:
                _git('read-tree', f'refs/remotes/origin/{BRANCH}')
            else:
                _git('read-tree', '--empty')
            _git('update-index', '--add', '--cacheinfo', f'100644,{blob},{app_id}.json')
            tree = _git('write-tree')
        finally:
            if index is None:
                os.environ.pop('GIT_INDEX_FILE', None)
            else:
                os.environ['GIT_INDEX_FILE'] = index
            Path(temporary_index).unlink(missing_ok=True)
        args = ['commit-tree', tree]
        if previous:
            args.extend(['-p', previous])
        commit = _git('-c', 'user.name=github-actions[bot]', '-c',
                      'user.email=41898282+github-actions[bot]@users.noreply.github.com',
                      *args, input_text=f'Update {app_id} compatibility badge\n')
        lease = f'refs/heads/{BRANCH}:{previous or ""}'
        pushed = subprocess.run(['git', 'push', 'origin', f'--force-with-lease={lease}',
                                 f'{commit}:refs/heads/{BRANCH}'], text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if pushed.returncode == 0:
            return True
        time.sleep(0.2)
    raise RuntimeError('Compatibility badge branch changed during all compare-and-swap attempts')


def publish_event(event_path, artifact_directory):
    event = _read_json(event_path, limit=EVENT_LIMIT)
    if not isinstance(event, dict):
        return False
    try:
        run = _event_run(event)
    except (KeyError, TypeError, AttributeError):
        return False
    if not is_trusted_run(run):
        return False
    artifact = Path(artifact_directory) / 'compatibility-badge.json'
    report = _read_json(artifact)
    try:
        validate_report(report)
    except (TypeError, ValueError):
        report = None
    result = classify(run, report)
    matched_report = (report is not None and all(report.get(field) == run.get(field)
                                                for field in ('appId', 'runId', 'runNumber',
                                                              'runAttempt', 'sha')))
    version_name = (report['versionName'] if matched_report and report['versionName']
                    and result['version'].startswith(f"{report['versionName']} · ") else None)
    record = {'schemaVersion': 1, **result, 'message': result['version'],
              'versionName': version_name,
              'versionCode': report['versionCode'] if version_name else None,
              'candidateSha256': report['candidateSha256'] if version_name else None,
              'appId': run['appId'],
              'runNumber': run['runNumber'], 'runAttempt': run['runAttempt'],
              'runId': run['runId'], 'sha': run['sha']}
    return publish(record, run['appId'])


def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest='command', required=True)
    write = commands.add_parser('write')
    write.add_argument('--context', type=Path, required=True)
    write.add_argument('--output', type=Path, required=True)
    adaptation = commands.add_parser('adaptation-summary')
    adaptation.add_argument('--task-directory', type=Path, required=True)
    adaptation.add_argument('--claimed', required=True)
    adaptation.add_argument('--model-outcome', default='')
    adaptation.add_argument('--needs-model', default='')
    adaptation.add_argument('--static-validation-outcome', default='')
    adaptation.add_argument('--app-id', choices=WORKFLOWS, required=True)
    adaptation.add_argument('--output', type=Path, required=True)
    publish_parser = commands.add_parser('publish')
    publish_parser.add_argument('--event', type=Path, required=True)
    publish_parser.add_argument('--artifact-directory', type=Path, required=True)
    args = parser.parse_args()
    if args.command == 'write':
        context = _read_json(args.context)
        if not isinstance(context, dict) or set(context) != {'run', 'state'}:
            raise ValueError('Compatibility badge context fields do not match schema')
        report = create_report(context['run'], context['state'])
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, separators=(',', ':')) + '\n')
    elif args.command == 'adaptation-summary':
        args.output.parent.mkdir(parents=True, exist_ok=True)
        write_adaptation_summary(args.task_directory, args.claimed, args.model_outcome,
                                 args.needs_model, args.static_validation_outcome, args.output,
                                 args.app_id)
    else:
        publish_event(args.event, args.artifact_directory)


if __name__ == '__main__':
    main()
