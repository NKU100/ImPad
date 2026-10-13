"""Capture and restore verified, candidate-specific adaptation checkpoints."""

import hashlib
import json
import os
import shutil
import subprocess
import argparse
from datetime import datetime, timezone
from pathlib import Path

from scripts.ci.app_policy import get_policy
from scripts.ci.codex_adaptation import candidate_key, validate_paths
from scripts.ci.discover_latest_wechat import append_github_output


SCHEMA_VERSION = 1


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def git(worktree, *args, env=None):
    return subprocess.check_output(['git', *args], cwd=worktree, env=env).decode().strip()


def _permitted(path, policy):
    return path == policy.targets_path or path in policy.shared_source_paths or path.startswith(policy.app_source_prefix)


def _read_json(path, fallback):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return fallback


def capture_checkpoint(directory, issue_context=None):
    directory = Path(directory)
    state = json.loads((directory / 'state.json').read_text())
    report = state['report']
    policy = get_policy(state.get('appId'))
    if report.get('appId', policy.app_id) != policy.app_id:
        raise ValueError('Checkpoint appId does not match the candidate')
    worktree = Path(state['worktree'])
    base = state.get('agent_base', state['base'])

    original_index = worktree / '.git/index'
    index_copy = directory / 'checkpoint.index'
    if original_index.is_file():
        shutil.copyfile(original_index, index_copy)
    index_env = dict(os.environ, GIT_INDEX_FILE=str(index_copy))
    try:
        subprocess.run(['git', 'add', '--intent-to-add', '--all'], cwd=worktree,
                       env=index_env, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        changed = [path for path in git(worktree, 'diff', '--name-only', base, env=index_env).splitlines() if path]
        permitted = [path for path in changed if _permitted(path, policy)]
        excluded = [path for path in changed if path not in permitted]
        links = [path for path in permitted if (worktree / path).is_symlink()]
        excluded.extend(links)
        permitted = [path for path in permitted if path not in links]
        validate_paths(permitted, policy.app_id)
        patch_path = directory / 'checkpoint.patch'
        if permitted:
            with patch_path.open('wb') as output:
                subprocess.run(['git', 'diff', '--binary', '--full-index', base, '--', *permitted],
                               cwd=worktree, env=index_env, check=True, stdout=output)
        else:
            patch_path.write_bytes(b'')
    finally:
        index_copy.unlink(missing_ok=True)

    validated_report = _read_json(directory / 'candidate-report.json', {})
    stage = ('static-verified-pending-runtime'
             if validated_report.get('status') == 'STATIC_VERIFIED_PENDING_RUNTIME'
             and (directory / 'adaptation.patch').is_file()
             else 'adaptation-in-progress')
    diagnostics = _read_json(directory / 'goal-diagnostics.json', None)
    runtime_report = _read_json(directory / 'runtime-report.json', None)
    resume = issue_context if issue_context is not None else _read_json(directory / 'resume-context.json', {})
    prior_handoff = resume.get('handoff', {}) if isinstance(resume, dict) else {}
    issue_resume = resume.get('issue', resume) if isinstance(resume, dict) else {}
    issue_fields = ('status', 'checkpointRunId', 'checkpointArtifact', 'existingPr', 'existingPrHead',
                    'previousRun', 'previousBlockers', 'ownerComments', 'consumedCommentId')
    from scripts.ci.adaptation_summary import read_summary
    handoff = {
        'summary': read_summary(worktree / 'work/analysis/adaptation-summary.json'),
        'issue': {key: issue_resume[key] for key in issue_fields if key in issue_resume}
        if isinstance(issue_resume, dict) else {},
        'diagnostics': diagnostics if diagnostics is not None else prior_handoff.get('diagnostics', {}),
        'runtime': runtime_report if runtime_report is not None else prior_handoff.get('runtime', {}),
    }
    handoff_path = directory / 'handoff.json'
    handoff_path.write_text(json.dumps(handoff, indent=2) + '\n')
    manifest = {
        'schemaVersion': SCHEMA_VERSION,
        'appId': policy.app_id,
        'candidateKey': candidate_key(report),
        'identity': report['identity'],
        'baseCommit': state['base'],
        'stage': stage,
        'patchSha256': sha256(patch_path),
        'handoffSha256': sha256(handoff_path),
        'excludedPaths': excluded,
        'updatedAt': datetime.now(timezone.utc).isoformat(),
    }
    native_path = directory / 'native-session.tar.age'
    native_thread = _read_json(directory / 'native-thread.json', {})
    if native_path.is_file() and native_thread.get('threadId'):
        manifest['nativeThreadId'] = native_thread['threadId']
        manifest['nativeSessionSha256'] = sha256(native_path)
    (directory / 'checkpoint.json').write_text(json.dumps(manifest, indent=2) + '\n')
    return manifest


def restore_checkpoint(directory, checkpoint_directory, issue_context=None):
    directory = Path(directory)
    checkpoint_directory = Path(checkpoint_directory)
    state = json.loads((directory / 'state.json').read_text())
    report = state['report']
    policy = get_policy(state.get('appId'))
    handoff_path = checkpoint_directory / 'handoff.json'
    handoff = _read_json(handoff_path, {})
    context = issue_context if issue_context is not None else _read_json(directory / 'resume-context.json', {})
    manifest_path = checkpoint_directory / 'checkpoint.json'
    if not manifest_path.is_file():
        legacy = _read_json(checkpoint_directory / 'goal-diagnostics.json', {})
        (directory / 'resume-context.json').write_text(json.dumps({
            'issue': context, 'legacyDiagnostics': legacy,
        }, indent=2) + '\n')
        return {'needsModel': True, 'stage': 'legacy-diagnostics-only'}

    manifest = json.loads(manifest_path.read_text())
    if manifest.get('schemaVersion') != SCHEMA_VERSION:
        raise ValueError('Unsupported adaptation checkpoint schema')
    if manifest.get('appId') != policy.app_id or manifest.get('candidateKey') != candidate_key(report):
        raise ValueError('Checkpoint belongs to a different app or candidate APK')
    if manifest.get('identity') != report.get('identity'):
        raise ValueError('Checkpoint candidate identity does not match the verified APK')
    patch_path = checkpoint_directory / 'checkpoint.patch'
    if not patch_path.is_file() or sha256(patch_path) != manifest.get('patchSha256'):
        raise ValueError('Checkpoint patch SHA-256 verification failed')
    if not handoff_path.is_file() or sha256(handoff_path) != manifest.get('handoffSha256'):
        raise ValueError('Checkpoint handoff SHA-256 verification failed')

    worktree = Path(state['worktree'])
    if patch_path.stat().st_size:
        subprocess.run(['git', 'apply', '--3way', '--index', str(patch_path)], cwd=worktree,
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        # Keep the restored checkpoint in the worktree, where the shared validator
        # and patch exporter expect uncommitted agent edits.
        subprocess.run(['git', 'reset', '--quiet'], cwd=worktree, check=True)
    tracked = git(worktree, 'diff', '--name-only', state.get('agent_base', state['base'])).splitlines()
    untracked = git(worktree, 'ls-files', '--others', '--exclude-standard').splitlines()
    validate_paths(tracked + untracked, policy.app_id)
    if any((worktree / path).is_symlink() for path in tracked + untracked):
        raise ValueError('Checkpoint must not restore symbolic links')

    if handoff.get('summary'):
        summary_path = worktree / 'work/analysis/adaptation-summary.json'
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path.write_text(json.dumps(handoff['summary']) + '\n')
    issue_status = context.get('status')
    needs_model = not (manifest.get('stage') == 'static-verified-pending-runtime'
                       and issue_status != 'RUNTIME_REJECTED' and not context.get('ownerComments'))
    restored = {
        'status': context.get('status', 'ADAPTATION_RUNNING'),
        'checkpointRunId': context.get('checkpointRunId', ''),
        'checkpointArtifact': context.get('checkpointArtifact', ''),
        'existingPr': context.get('existingPr', ''),
        'existingPrHead': context.get('existingPrHead', ''),
        'previousBlockers': context.get('previousBlockers', []),
        'ownerComments': context.get('ownerComments', []),
        'consumedCommentId': context.get('consumedCommentId', 0),
        'previousStage': manifest.get('stage'),
        'handoff': handoff,
        'baseCommit': manifest.get('baseCommit'),
    }
    if manifest.get('nativeThreadId'):
        from scripts.ci.codex_session_checkpoint import restore_session
        session = checkpoint_directory / 'native-session.tar.age'
        if not session.is_file() or sha256(session) != manifest.get('nativeSessionSha256'):
            raise ValueError('Native session SHA-256 verification failed')
        restore_session(checkpoint_directory, Path(os.environ['CODEX_HOME']),
                        os.environ.get('CODEX_CHECKPOINT_IDENTITY', ''),
                        os.environ.get('CODEX_CHECKPOINT_RECIPIENT', ''))
        restored['nativeThreadId'] = manifest['nativeThreadId']
        shutil.copyfile(session, directory / session.name)
        (directory / 'native-thread.json').write_text(json.dumps({'threadId': manifest['nativeThreadId']}) + '\n')
    (directory / 'resume-context.json').write_text(json.dumps(restored, indent=2) + '\n')
    return {'needsModel': needs_model, 'stage': manifest.get('stage')}


def reuse_published_checkpoint(directory):
    """Never combine interrupted code with an earlier native-session snapshot."""
    directory = Path(directory).resolve()
    pointer = _read_json(directory / 'last-published-checkpoint.json', {})
    source = Path(pointer.get('directory', '/unavailable')).resolve()
    if directory not in source.parents or not (source / 'checkpoint.json').is_file():
        return None
    for name in ('checkpoint.json', 'checkpoint.patch', 'handoff.json', 'native-session.tar.age'):
        shutil.copyfile(source / name, directory / name)
    return json.loads((directory / 'checkpoint.json').read_text())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('stage', choices=['capture', 'restore'])
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--last-published', action='store_true')
    parser.add_argument('--checkpoint-directory', type=Path)
    args = parser.parse_args()
    if args.stage == 'capture':
        manifest = reuse_published_checkpoint(args.directory) if args.last_published else capture_checkpoint(args.directory)
        if manifest is None:
            result = {'checkpoint_ready': 'false'}
            if os.environ.get('GITHUB_OUTPUT'):
                append_github_output(Path(os.environ['GITHUB_OUTPUT']), result)
            print(json.dumps(result))
            return
        result = {'checkpoint_ready': 'true', 'checkpoint_stage': manifest['stage']}
    else:
        result = restore_checkpoint(args.directory, args.checkpoint_directory or args.directory)
        result = {'needs_model': str(result['needsModel']).lower(), 'checkpoint_stage': result['stage']}
    output = os.environ.get('GITHUB_OUTPUT')
    if output:
        append_github_output(Path(output), result)
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
