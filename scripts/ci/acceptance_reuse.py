"""Reuse immutable acceptance evidence only for an unchanged validation input."""
import argparse
import hashlib
import json
from pathlib import Path

from scripts.ci.acceptance import read_semantic_acceptance_table, assert_evidence_archive_unchanged
from scripts.ci.compatibility_badge import classify_regression
from scripts.ci.discover_latest_wechat import append_github_output


def runtime_validation_sha256(repository: Path) -> str:
    repository = Path(repository).resolve()
    digest = hashlib.sha256()
    # Match the runtime files covered by the module source fingerprint. An
    # acceptance-only commit changes neither this digest nor module execution.
    paths = [repository / '.github/workflows/runtime-validation.yml']
    for directory in ('scripts/ci', '.github/actions/prepare-dexkit-system-deps'):
        paths.extend((repository / directory).rglob('*'))
    for path in sorted(paths):
        if '__pycache__' in path.parts or path.suffix == '.pyc' or not path.is_file():
            continue
        if path.is_symlink():
            raise ValueError('Runtime validation input cannot be a symbolic link')
        relative = path.relative_to(repository).as_posix()
        digest.update(relative.encode() + b'\0' + hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def acceptance_matches_candidate(record, candidate, static):
    return (record.get('status') == 'VERIFIED'
            and record.get('identity') == candidate.get('identity')
            and [(row['role'], row['sha256']) for row in record.get('apkFiles', [])]
            == [(row['role'], row['sha256']) for row in candidate.get('apkFiles', [])]
            and record.get('completePlan') == candidate.get('expectedPlan')
            and record.get('executionSha256') == candidate.get('expectedExecutionSha256')
            and record.get('recognitionSha256') == candidate.get('recognitionSha256')
            and record['staticReport']['executionIdentity'] == static['executionIdentity'])


def find_reusable_acceptance(repository: Path, candidate: dict, static: dict) -> dict | None:
    repository = Path(repository).resolve()
    app_id = candidate['appId']
    if candidate.get('status') != 'PREPARED':
        return None
    raw = read_semantic_acceptance_table(
        repository / 'compatibility' / app_id / 'acceptance-v2.json', app_id, repository)
    if raw is None:
        return None
    runtime_sha = runtime_validation_sha256(repository)
    for record in json.loads(raw)['records']:
        if not acceptance_matches_candidate(record, candidate, static):
            continue
        assert_evidence_archive_unchanged(repository, record['evidenceArchive'])
        runtime = json.loads((repository / record['rawStageEvidence']['runtime']['path']).read_text())
        if (runtime.get('status') == 'RUNTIME_SMOKE_VERIFIED'
                and runtime.get('runtimeValidationSha256') == runtime_sha):
            return record
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--repository', type=Path, default=Path.cwd())
    parser.add_argument('--github-output', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    candidate = json.loads((args.directory / 'candidate-report.json').read_text())
    static_path = (args.directory / candidate['staticReport']).resolve()
    if not static_path.is_relative_to(args.directory.resolve()):
        raise ValueError('Static report escapes candidate directory')
    static_bytes = static_path.read_bytes()
    if hashlib.sha256(static_bytes).hexdigest() != candidate['staticReportSha256']:
        raise ValueError('Static report changed before acceptance reuse')
    regression = json.loads((args.directory / 'regression/results.json').read_text())
    record = None
    reason = 'No unchanged verified acceptance record.'
    if classify_regression(regression) == 'PASSED':
        try:
            static = json.loads(static_bytes)
            record = find_reusable_acceptance(args.repository, candidate, static)
            if record:
                from scripts.ci.runtime_task import _semantic_stage_context
                regression_path = (args.directory / 'regression/results.json').resolve()
                _semantic_stage_context(candidate, record['module'], static, {
                    'records': regression, 'evidence': {'path': str(regression_path),
                        'sha256': hashlib.sha256(regression_path.read_bytes()).hexdigest()},
                }, args.repository)
        except (OSError, ValueError, KeyError, TypeError) as error:
            record = None
            reason = f'Acceptance evidence cannot be reused: {error}'
    else:
        reason = 'Current fixed-record regression did not pass.'
    outputs = {'acceptance_reused': 'true' if record else 'false'}
    if record:
        outputs.update(outcome='VERIFIED', record_id=record['recordId'],
                       version_name=str(candidate['identity']['versionName']),
                       version_code=str(candidate['identity']['versionCode']),
                       candidate_sha256=candidate['identity']['apkSha256'])
        reason = f"Reused verified acceptance `{record['recordId']}`. Current static checks and fixed regression passed; module build and ReDroid were skipped."
    append_github_output(args.github_output, outputs)
    with args.report.open('a') as report:
        report.write(f'\n## Acceptance reuse\n\n{reason}\n')
    print(reason)


if __name__ == '__main__':
    main()
