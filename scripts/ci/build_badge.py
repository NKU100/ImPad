"""Create and publish the trusted main-branch build badge record."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET


REPOSITORY = 'NKU100/ImPad'
WORKFLOW_PATH = '.github/workflows/build.yml'
REPORT_LIMIT = 4096
SHA_PATTERN = re.compile(r'^[0-9a-f]{40}$')
REPORT_FIELDS = {'schemaVersion', 'runId', 'runAttempt', 'sha', 'unitTests',
                 'staticRegression', 'moduleBuild', 'artifactUpload'}
BUILD_TASK = re.compile(r'^:[^:]+:(?:compile\w+|kapt\w*|ksp\w*|minify\w*WithR8|package\w*|sign\w*|validateSigning\w*)$')
CHECK_TASK = re.compile(r'^:[^:]+:verify(?:Release)?ModuleMetadata$')
EXECUTION_CAUSE = re.compile(
    r'outofmemory|out of memory|ioexception|ioerror|filesystemexception|file.?system|'
    r'daemon|disk.?full|no space|broken pipe|process.*(?:died|exit|terminated)|killed', re.I)
OUTCOMES = {
    'unitTests': {'passed', 'failed', 'unknown'},
    'staticRegression': {'passed', 'check_failure', 'execution_failure', 'unknown'},
    'moduleBuild': {'passed', 'failed', 'unknown'},
    'artifactUpload': {'passed', 'failed', 'unknown'},
}


def validate_report(report):
    encoded = json.dumps(report, separators=(',', ':')).encode()
    if len(encoded) > REPORT_LIMIT:
        raise ValueError('Badge evidence exceeds the size limit')
    if not isinstance(report, dict) or set(report) != REPORT_FIELDS:
        raise ValueError('Badge evidence fields do not match schema version 1')
    if type(report['schemaVersion']) is not int or report['schemaVersion'] != 1:
        raise ValueError('Unsupported badge evidence schema')
    for field in ('runId', 'runAttempt'):
        if type(report[field]) is not int or report[field] <= 0:
            raise ValueError(f'Invalid {field}')
    if not isinstance(report['sha'], str) or not SHA_PATTERN.fullmatch(report['sha']):
        raise ValueError('Invalid build commit SHA')
    for field, allowed in OUTCOMES.items():
        if report[field] not in allowed:
            raise ValueError(f'Invalid {field} outcome')
    return report


def is_trusted_run(run):
    return (run.get('repository') == REPOSITORY
            and run.get('headRepository') == REPOSITORY
            and run.get('workflowPath') == WORKFLOW_PATH
            and run.get('event') == 'push'
            and run.get('branch') == 'main'
            and run.get('status') == 'completed'
            and all(type(run.get(field)) is int and run[field] > 0
                    for field in ('runId', 'runNumber', 'runAttempt'))
            and isinstance(run.get('sha'), str) and bool(SHA_PATTERN.fullmatch(run['sha'])))


def classify(run, report):
    if run is None:
        return {'state': 'gray', 'label': '构建', 'message': '暂无记录', 'color': 'lightgrey'}
    conclusion = run.get('conclusion')
    if conclusion == 'cancelled':
        return {'state': 'gray', 'label': '构建', 'message': '已取消', 'color': 'lightgrey'}
    if conclusion == 'timed_out':
        return {'state': 'gray', 'label': '构建', 'message': '执行异常', 'color': 'lightgrey'}
    try:
        validate_report(report)
    except (TypeError, ValueError):
        report = None
    if (report is None or report['runId'] != run.get('runId')
            or report['runAttempt'] != run.get('runAttempt')
            or report['sha'] != run.get('sha')):
        return {'state': 'gray', 'label': '构建', 'message': '执行异常', 'color': 'lightgrey'}

    if report['unitTests'] == 'failed' or report['staticRegression'] == 'check_failure':
        return {'state': 'red', 'label': '构建', 'message': '检查失败', 'color': 'red'}
    if report['moduleBuild'] == 'failed':
        return {'state': 'red', 'label': '构建', 'message': '构建失败', 'color': 'red'}
    if (conclusion == 'success' and report['unitTests'] == 'passed'
            and report['staticRegression'] == 'passed'
            and report['moduleBuild'] == 'passed'
            and report['artifactUpload'] == 'passed'):
        return {'state': 'green', 'label': '构建', 'message': '通过', 'color': 'brightgreen'}
    return {'state': 'gray', 'label': '构建', 'message': '执行异常', 'color': 'lightgrey'}


def should_replace(current, incoming):
    if current is None:
        return True
    if not isinstance(current, dict) or not isinstance(incoming, dict):
        return False
    old = (current.get('runNumber', 0), current.get('runAttempt', 0))
    new = (incoming.get('runNumber', 0), incoming.get('runAttempt', 0))
    return new > old


def unit_tests_outcome(result):
    if not isinstance(result, dict):
        return 'unknown'
    counts = [result.get(key) for key in ('testsRun', 'failures', 'errors')]
    unexpected = result.get('unexpectedSuccesses', 0)
    stopped = result.get('shouldStop', False)
    successful = result.get('wasSuccessful')
    if any(type(value) is not int or value < 0 for value in counts):
        return 'unknown'
    if type(unexpected) is not int or unexpected < 0 or type(stopped) is not bool:
        return 'unknown'
    if successful is not None and type(successful) is not bool:
        return 'unknown'
    tests, failures, errors = counts
    if failures or unexpected:
        return 'failed'
    if errors or stopped or successful is False or tests == 0:
        return 'unknown'
    return 'passed'


def gradle_test_failures(results_root, started_ns):
    files = [path for path in Path(results_root).glob('*/build/test-results/**/*.xml')
             if path.stat().st_mtime_ns >= started_ns]
    if not files:
        return None
    try:
        return sum(len(root.findall('.//failure')) for path in files
                   for root in [ET.parse(path).getroot()])
    except (OSError, ET.ParseError):
        return None


def gradle_failure_outcome(evidence):
    if (not isinstance(evidence, dict) or type(evidence.get('schemaVersion')) is not int
            or evidence.get('schemaVersion') != 1):
        return 'unknown'
    tasks = evidence.get('failedTasks')
    causes = evidence.get('causes')
    if (not isinstance(tasks, list) or not isinstance(causes, list)
            or len(tasks) > 32 or len(causes) > 32
            or any(not isinstance(item, str) or len(item) > 300 for item in tasks + causes)):
        return 'unknown'
    if any(re.search(r'resolv|artifact|download|http|socket|ssl|connect|network|unknown.?host|timeout|dns|resource', cause, re.I)
           or EXECUTION_CAUSE.search(cause)
           for cause in causes):
        return 'unknown'
    if any(CHECK_TASK.fullmatch(task) for task in tasks):
        return 'check_failure'
    known_build_failure = any(re.search(
        r'(?:CompilationFailedException|CompilationErrorException|KotlinCompilationException|'
        r'CompilationError|SigningException|ApkCreationException)$', cause) for cause in causes)
    return 'build_failure' if known_build_failure and any(
        BUILD_TASK.fullmatch(task) for task in tasks) else 'unknown'


def _read_json(path, limit=REPORT_LIMIT):
    try:
        source = Path(path)
        if source.is_symlink():
            return None
        with source.open('rb') as input_file:
            raw = input_file.read(limit + 1)
        if len(raw) > limit:
            return None
        return json.loads(raw)
    except (OSError, ValueError, TypeError):
        return None


def _static_outcome(path):
    rows = _read_json(path, limit=2 * 1024 * 1024)
    if not isinstance(rows, list) or not rows:
        return 'unknown'
    failures = [row for row in rows if not isinstance(row, dict) or row.get('status') != 'PASSED']
    if not failures:
        return 'passed'
    kinds = {row.get('failureKind') for row in failures
             if isinstance(row, dict) and row.get('failureKind') in {'check_failure', 'execution_failure'}}
    if any(not isinstance(row, dict) or row.get('failureKind') not in
           {'check_failure', 'execution_failure'} for row in failures):
        return 'unknown'
    if 'check_failure' in kinds:
        return 'check_failure'
    if 'execution_failure' in kinds or not kinds:
        return 'execution_failure'
    return 'unknown'


def _outcome(value, success='passed', failure='unknown'):
    return success if value == 'success' else failure if value == 'failure' else 'unknown'


def write_evidence(output):
    tests = _read_json('work/build-badge/unit-tests.json')
    unit = unit_tests_outcome(tests)
    gradle = os.environ.get('GRADLE_TESTS_OUTCOME', 'unknown')
    try:
        started_ns = int(Path('work/build-badge/gradle-tests-started').read_text())
    except (OSError, ValueError):
        started_ns = 0
    failures = gradle_test_failures('.', started_ns) if started_ns else None
    if failures:
        unit = 'failed'
    elif gradle != 'success' and unit != 'failed':
        unit = 'unknown'
    static = _static_outcome('work/static-regression/results.json')
    module = _read_json('work/build-badge/module-build.json')
    module_outcome = module.get('outcome') if isinstance(module, dict) else 'unknown'
    gradle_failure = gradle_failure_outcome(
        _read_json('work/build-badge/gradle-failures.json', limit=32768))
    if gradle_failure == 'check_failure':
        unit = 'failed'
    elif gradle_failure == 'build_failure':
        module_outcome = 'failed'
    if module_outcome not in OUTCOMES['moduleBuild']:
        module_outcome = 'unknown'
    upload = _outcome(os.environ.get('MODULE_UPLOAD_OUTCOME'), failure='failed')
    report = {
        'schemaVersion': 1,
        'runId': int(os.environ['GITHUB_RUN_ID']),
        'runAttempt': int(os.environ['GITHUB_RUN_ATTEMPT']),
        'sha': os.environ['GITHUB_SHA'],
        'unitTests': unit,
        'staticRegression': static,
        'moduleBuild': module_outcome,
        'artifactUpload': upload,
    }
    validate_report(report)
    destination = Path(output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, separators=(',', ':')) + '\n')


def _event_run(event):
    run = event['workflow_run']
    repo = event['repository']['full_name']
    head = run.get('head_repository') or {}
    return {
        'repository': repo,
        'headRepository': head.get('full_name'),
        'workflowPath': run.get('path'),
        'event': run.get('event'),
        'branch': run.get('head_branch'),
        'status': run.get('status'),
        'conclusion': run.get('conclusion'),
        'runId': run.get('id'),
        'runNumber': run.get('run_number'),
        'runAttempt': run.get('run_attempt'),
        'sha': run.get('head_sha'),
    }


def validate_record(record):
    fields = {'schemaVersion', 'state', 'label', 'message', 'color', 'runNumber',
              'runAttempt', 'runId', 'sha'}
    if not isinstance(record, dict) or set(record) != fields:
        raise ValueError('Published badge record fields do not match schema version 1')
    if (type(record['schemaVersion']) is not int or record['schemaVersion'] != 1
            or record['state'] not in {'green', 'red', 'gray'}):
        raise ValueError('Invalid published badge state')
    if record['label'] != '构建' or record['color'] not in {'brightgreen', 'red', 'lightgrey'}:
        raise ValueError('Invalid published badge appearance')
    if not isinstance(record['message'], str) or len(record['message']) > 24:
        raise ValueError('Invalid published badge message')
    allowed_appearance = {
        'green': {('通过', 'brightgreen')},
        'red': {('检查失败', 'red'), ('构建失败', 'red')},
        'gray': {('已取消', 'lightgrey'), ('执行异常', 'lightgrey'), ('暂无记录', 'lightgrey')},
    }
    if (record['message'], record['color']) not in allowed_appearance[record['state']]:
        raise ValueError('Published badge state and appearance disagree')
    for field in ('runNumber', 'runAttempt', 'runId'):
        if type(record[field]) is not int or record[field] <= 0:
            raise ValueError(f'Invalid {field}')
    if not isinstance(record['sha'], str) or not SHA_PATTERN.fullmatch(record['sha']):
        raise ValueError('Invalid published build commit SHA')
    return record


def _git(*args, input_text=None):
    return subprocess.run(['git', *args], check=True, text=True, input=input_text,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.strip()


def _remote_current():
    ref = 'refs/remotes/origin/build-badge-data'
    fetched = subprocess.run(['git', 'fetch', '--no-tags', 'origin',
                              f'refs/heads/build-badge-data:{ref}'], text=True,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if fetched.returncode:
        return None, None
    commit = _git('rev-parse', ref)
    raw = subprocess.run(['git', 'show', f'{ref}:build.json'], text=True,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if raw.returncode or len(raw.stdout.encode()) > REPORT_LIMIT:
        return commit, None
    try:
        current = json.loads(raw.stdout)
    except ValueError:
        current = None
    if current is not None:
        try:
            validate_record(current)
        except (TypeError, ValueError):
            current = None
    return commit, current


def publish(record, attempts=5):
    validate_record(record)
    payload = json.dumps(record, separators=(',', ':')) + '\n'
    if len(payload.encode()) > REPORT_LIMIT:
        raise ValueError('Published badge record exceeds the size limit')
    for _ in range(attempts):
        previous, current = _remote_current()
        if current is not None and not should_replace(current, record):
            return False
        tree = _git('hash-object', '-w', '--stdin', input_text=payload)
        index = os.environ.get('GIT_INDEX_FILE')
        temporary_index = str(Path('work/build-badge') / 'index')
        Path(temporary_index).parent.mkdir(parents=True, exist_ok=True)
        os.environ['GIT_INDEX_FILE'] = temporary_index
        try:
            _git('read-tree', '--empty')
            _git('update-index', '--add', '--cacheinfo', f'100644,{tree},build.json')
            root_tree = _git('write-tree')
        finally:
            if index is None:
                os.environ.pop('GIT_INDEX_FILE', None)
            else:
                os.environ['GIT_INDEX_FILE'] = index
            Path(temporary_index).unlink(missing_ok=True)
        args = ['commit-tree', root_tree]
        if previous:
            args.extend(['-p', previous])
        commit = _git('-c', 'user.name=github-actions[bot]', '-c',
                      'user.email=41898282+github-actions[bot]@users.noreply.github.com',
                      *args, input_text='Update main build badge\n')
        lease = f'refs/heads/build-badge-data:{previous or ""}'
        pushed = subprocess.run(['git', 'push', 'origin', f'--force-with-lease={lease}',
                                 f'{commit}:refs/heads/build-badge-data'], text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if pushed.returncode == 0:
            return True
        time.sleep(0.2)
    raise RuntimeError('Badge branch changed during all compare-and-swap attempts')


def publish_event(event_path, artifact_directory):
    event = json.loads(Path(event_path).read_text())
    run = _event_run(event)
    if not is_trusted_run(run):
        return False
    artifact = Path(artifact_directory) / 'build-badge.json'
    report = None if artifact.is_symlink() else _read_json(artifact)
    state = classify(run, report)
    record = {'schemaVersion': 1, **state, 'runNumber': run.get('runNumber'), 'runAttempt': run.get('runAttempt'),
              'runId': run.get('runId'), 'sha': run.get('sha')}
    return publish(record)


def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest='command', required=True)
    write = commands.add_parser('write')
    write.add_argument('--output', type=Path, default=Path('work/build-badge/build-badge.json'))
    publish_parser = commands.add_parser('publish')
    publish_parser.add_argument('--event', type=Path, required=True)
    publish_parser.add_argument('--artifact-directory', type=Path, required=True)
    gradle_parser = commands.add_parser('gradle-failure')
    gradle_parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    if args.command == 'write':
        write_evidence(args.output)
    elif args.command == 'publish':
        publish_event(args.event, args.artifact_directory)
    else:
        evidence = _read_json(args.report, limit=32768)
        raise SystemExit(0 if gradle_failure_outcome(evidence) == 'build_failure' else 1)


if __name__ == '__main__':
    main()
