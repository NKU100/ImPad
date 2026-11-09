"""Register a trusted official-candidate result without requiring an adaptation PR."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

from scripts.ci.acceptance import read_semantic_acceptance_table, assert_evidence_archive_unchanged
from scripts.ci.acceptance_reuse import (acceptance_matches_candidate, find_reusable_acceptance,
                                        runtime_validation_sha256)
from scripts.ci.app_policy import get_policy
from scripts.ci.discover_latest_wechat import append_github_output, validate_official_apk_url
from scripts.ci.runtime_task import register_cli


def registration_is_eligible(*, event_name, ref, actor, owner, operation, apk_url,
                             module_run_id, module_artifact, outcome, reused,
                             pending_adaptation):
    return (event_name in {'schedule', 'workflow_dispatch'} and ref == 'refs/heads/main'
            and bool(owner) and actor.casefold() == owner.casefold()
            and operation == 'adapt' and not (apk_url or module_run_id or module_artifact)
            and outcome == 'VERIFIED' and reused != 'true' and pending_adaptation != 'true')


def git(repository, *arguments):
    return subprocess.check_output(['git', '-C', str(repository), *arguments], text=True).strip()


def main_head_is_current(repository, expected_head):
    return (git(repository, 'rev-parse', 'HEAD') == expected_head
            and git(repository, 'ls-remote', 'origin', 'refs/heads/main').split()[0] == expected_head)


def commit_registered_acceptance(repository, app_id, registered, expected_head):
    repository = Path(repository).resolve()
    expected_path = f'compatibility/{app_id}/acceptance-v2.json'
    if registered.get('status') != 'REGISTERED' or registered.get('acceptancePath') != expected_path:
        raise ValueError('Registration did not produce the expected application acceptance table')
    raw = read_semantic_acceptance_table(repository / expected_path, app_id, repository)
    matches = [row for row in json.loads(raw)['records'] if row['recordId'] == registered.get('recordId')]
    if len(matches) != 1:
        raise ValueError('Registered record is missing or ambiguous')
    archive = matches[0]['evidenceArchive']
    assert_evidence_archive_unchanged(repository, archive)
    expected_evidence = [row['path'] for row in archive['files']] + [archive['manifestPath']]
    if (registered.get('evidencePaths') != expected_evidence
            or registered.get('evidenceManifest') != {
                'path': archive['manifestPath'], 'sha256': archive['manifestSha256']}):
        raise ValueError('Registration output differs from its verified evidence archive')
    digests = {expected_path: hashlib.sha256(raw).hexdigest(),
               **{row['path']: row['sha256'] for row in archive['files']},
               archive['manifestPath']: archive['manifestSha256']}
    if git(repository, 'diff', '--cached', '--name-only'):
        raise ValueError('Registration requires an empty staging area')
    if not main_head_is_current(repository, expected_head):
        return False
    subprocess.run(['git', '-C', str(repository), 'add', '--', *digests], check=True)
    staged = subprocess.check_output(
        ['git', '-C', str(repository), 'diff', '--cached', '--name-only', '-z']).decode().split('\0')[:-1]
    if set(staged) != set(digests):
        raise ValueError('Staged registration contains unexpected paths or no acceptance change')
    for path, digest in digests.items():
        blob = subprocess.check_output(['git', '-C', str(repository), 'show', ':' + path])
        if hashlib.sha256(blob).hexdigest() != digest:
            raise ValueError('Staged evidence differs from the validated registration')
    if git(repository, 'rev-parse', 'HEAD') != expected_head:
        raise ValueError('Tested source changed before acceptance commit')
    git(repository, 'config', 'user.name', 'github-actions[bot]')
    git(repository, 'config', 'user.email', '41898282+github-actions[bot]@users.noreply.github.com')
    subprocess.run(['git', '-C', str(repository), 'commit', '-m', f'Record {app_id} runtime acceptance'], check=True)
    # A normal push rejects a concurrent main update; never rewrite its history.
    subprocess.run(['git', '-C', str(repository), 'push', 'origin', 'HEAD:refs/heads/main'], check=True)
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--app-id', required=True)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--repository', type=Path, default=Path.cwd())
    parser.add_argument('--expected-head', required=True)
    parser.add_argument('--github-output', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    eligible = registration_is_eligible(
        event_name=os.environ.get('GITHUB_EVENT_NAME', ''), ref=os.environ.get('GITHUB_REF', ''),
        actor=os.environ.get('GITHUB_ACTOR', ''), owner=os.environ.get('GITHUB_REPOSITORY_OWNER', ''),
        operation=os.environ.get('OPERATION', ''), apk_url=os.environ.get('APK_URL', ''),
        module_run_id=os.environ.get('MODULE_RUN_ID', ''), module_artifact=os.environ.get('MODULE_ARTIFACT', ''),
        outcome=os.environ.get('VALIDATION_OUTCOME', ''), reused=os.environ.get('ACCEPTANCE_REUSED', ''),
        pending_adaptation=os.environ.get('PENDING_ADAPTATION', ''))
    if not eligible:
        raise ValueError('Only trusted official adaptation checks can register on main')
    if not re.fullmatch(r'[0-9a-f]{40}', args.expected_head):
        raise ValueError('Tested source head is malformed')
    repository = args.repository.resolve()
    directory = args.directory.resolve()

    def finish(status, message):
        append_github_output(args.github_output, {'registration_status': status})
        with args.report.open('a') as summary:
            summary.write(f'\n## Official candidate acceptance\n\n{message}\n')
        print(message)

    if not main_head_is_current(repository, args.expected_head):
        finish('STALE_HEAD', 'Main changed after validation; registration is deferred to the next check.')
        return
    candidate_path = directory / 'inputs/candidate-report.json'
    candidate = json.loads(candidate_path.read_text())
    if candidate.get('appId') != args.app_id:
        raise ValueError('Downloaded candidate belongs to another application')
    validate_official_apk_url(candidate.get('sourceUrl', ''), get_policy(args.app_id).official_apk_prefix)
    static_path = (candidate_path.parent / candidate['staticReport']).resolve()
    if not static_path.is_relative_to(candidate_path.parent):
        raise ValueError('Static report escapes the downloaded candidate')
    static = json.loads(static_path.read_text())
    smoke_path = directory / 'evidence/runtime-smoke/evidence/smoke-report.json'
    smoke = json.loads(smoke_path.read_text())
    if smoke.get('runtimeValidationSha256') != runtime_validation_sha256(repository):
        raise ValueError('Runtime evidence does not bind the current validation implementation')
    if find_reusable_acceptance(repository, candidate, static):
        finish('ALREADY_REGISTERED', 'This exact candidate and execution already have verified acceptance.')
        return
    result = directory / 'registered.json'
    register_cli([
        '--app-id', args.app_id, '--candidate', str(candidate_path),
        '--module-apk', str(directory / 'inputs/module/app-debug.apk'),
        '--regression', str(directory / 'inputs/regression/results.json'),
        '--smoke', str(smoke_path), '--evidence-root', str(directory / 'evidence/runtime-smoke'),
        '--acceptance', str(repository / 'compatibility' / args.app_id / 'acceptance.json'),
        '--repository', str(repository), '--conclusion', 'success',
        '--expected-head', args.expected_head, '--output', str(result),
    ])
    registered = json.loads(result.read_text())
    committed = commit_registered_acceptance(repository, args.app_id, registered, args.expected_head)
    if committed:
        finish('REGISTERED', f"Registered acceptance `{registered['recordId']}` on main.")
    else:
        finish('STALE_HEAD', 'Main changed before publication; registration is deferred to the next check.')


if __name__ == '__main__':
    main()
