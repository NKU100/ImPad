"""Exercise Codex thread and blocked Goal restoration without a model turn."""

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path


class AppServer:
    def __init__(self, codex_home):
        self.codex_home = Path(codex_home).resolve()
        self.codex_home.mkdir(parents=True, exist_ok=True)
        home = self.codex_home.parent
        home.mkdir(parents=True, exist_ok=True)
        allowed = ('PATH', 'LANG', 'LC_ALL', 'TMPDIR', 'SSL_CERT_FILE')
        self.env = {name: os.environ[name] for name in allowed if name in os.environ}
        self.env.update(HOME=str(home), CODEX_HOME=str(self.codex_home))
        self.process = subprocess.Popen(
            ['codex', 'app-server', '--listen', 'stdio://', '-c', 'features.goals=true',
             '-c', 'features.multi_agent=false', '-c', 'features.multi_agent_v2=false'],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=self.env,
        )
        self.request_id = 0
        self.request('initialize', {
            'clientInfo': {'name': 'impad_thread_resume_smoke', 'version': '1'},
            'capabilities': {'experimentalApi': True},
        })
        self.notify('initialized')

    def notify(self, method, params=None):
        self.process.stdin.write(json.dumps({'method': method, 'params': params or {}}) + '\n')
        self.process.stdin.flush()

    def request(self, method, params):
        self.request_id += 1
        request_id = self.request_id
        self.process.stdin.write(json.dumps({
            'id': request_id, 'method': method, 'params': params,
        }) + '\n')
        self.process.stdin.flush()
        while True:
            line = self.process.stdout.readline()
            if not line:
                details = self.process.stderr.read()[-2000:]
                raise RuntimeError(f'Codex app-server exited during {method}: {details}')
            response = json.loads(line)
            if response.get('id') != request_id:
                continue
            if 'error' in response:
                raise RuntimeError(f'{method} failed: {response["error"]}')
            return response.get('result', {})

    def close(self):
        if self.process.poll() is not None:
            return
        self.process.stdin.close()
        try:
            status = self.process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            status = self.process.wait(timeout=5)
        if status:
            raise RuntimeError(f'Codex app-server exited with status {status}')


def issue_snapshot(path):
    issue = json.loads(path.read_text())
    body = issue.get('body') or ''
    marker = re.search(r'^<!-- impad-adaptation:(\S+) -->$', body, re.MULTILINE)
    status = re.search(r'^Status: `([A-Z_]+)`$', body, re.MULTILINE)
    version = re.search(r'^WeCom ([^\s]+) \((\d+)\)$', body, re.MULTILINE)
    apk_hash = re.search(r'^APK SHA-256: `([0-9a-f]{64})`$', body, re.MULTILINE)
    source = re.search(r'^Source: (https://\S+)$', body, re.MULTILINE)
    if not all((marker, status, version, apk_hash, source)):
        raise ValueError('Issue does not contain the expected WeCom adaptation fields')
    if issue.get('number') != 8 or issue.get('state') != 'open':
        raise ValueError('Expected open ImPad issue #8')
    if not marker.group(1).startswith('wecom-com.tencent.wework-arm64-v8a-'):
        raise ValueError('Issue #8 does not identify a WeCom ARM64 candidate')
    if apk_hash.group(1) not in marker.group(1) or apk_hash.group(1) not in issue.get('title', ''):
        raise ValueError('Issue candidate marker, title, and APK hash do not agree')
    return {
        'number': issue['number'],
        'title': issue['title'],
        'state': issue['state'],
        'url': issue['html_url'],
        'candidateKey': marker.group(1),
        'status': status.group(1),
        'versionName': version.group(1),
        'versionCode': version.group(2),
        'apkSha256': apk_hash.group(1),
        'sourceUrl': source.group(1),
    }


def create(args):
    issue = issue_snapshot(args.issue_json) if args.issue_json else None
    server = AppServer(args.codex_home)
    try:
        thread = server.request('thread/start', {
            'cwd': str(args.workspace.resolve()),
            'model': args.model,
            'approvalPolicy': 'never',
            'sandbox': 'workspace-write',
            'ephemeral': False,
            'config': {'features.goals': True},
        })['thread']
        thread_id = thread['id']
        objective = ('Resume adaptation record from GitHub issue '
                     f"#{issue['number']} for WeCom {issue['versionName']} "
                     f"({issue['versionCode']}, APK SHA-256 {issue['apkSha256']})."
                     if issue else
                     'Thread resume smoke test. Preserve this blocked Goal across CI jobs.')
        server.request('thread/goal/set', {
            'threadId': thread_id,
            'objective': objective,
            'status': 'blocked',
            'origin': 'automatic',
        })
        args.metadata.parent.mkdir(parents=True, exist_ok=True)
        args.metadata.write_text(json.dumps({
            'threadId': thread_id,
            'objective': objective,
            'workspace': str(args.workspace.resolve()),
            'expectedStatus': 'blocked',
            'issue': issue,
        }, indent=2) + '\n')
    finally:
        server.close()
    auth_file = Path(args.codex_home) / 'auth.json'
    if auth_file.exists():
        raise RuntimeError('Refusing to checkpoint Codex authentication')
    print('Created a blocked Goal without starting a model turn.')


def resume(args):
    metadata = json.loads(args.metadata.read_text())
    issue = issue_snapshot(args.issue_json) if args.issue_json else None
    if metadata.get('issue') != issue:
        raise RuntimeError('Issue #8 changed between checkpoint creation and restoration')
    server = AppServer(args.codex_home)
    try:
        thread = server.request('thread/resume', {
            'threadId': metadata['threadId'],
            'cwd': str(args.workspace.resolve()),
        })['thread']
        if thread.get('id') != metadata['threadId']:
            raise RuntimeError('Restored thread ID does not match the checkpoint')
        goal = server.request('thread/goal/get', {'threadId': metadata['threadId']}).get('goal') or {}
        if goal.get('status') != metadata['expectedStatus']:
            raise RuntimeError(f'Expected blocked Goal, got {goal.get("status")}')
        if goal.get('objective') != metadata['objective']:
            raise RuntimeError('Restored Goal objective does not match the Issue #8 checkpoint')
        server.request('thread/goal/set', {
            'threadId': metadata['threadId'],
            'status': 'active',
            'origin': 'user',
        })
        goal = server.request('thread/goal/get', {'threadId': metadata['threadId']}).get('goal') or {}
        if goal.get('status') != 'active':
            raise RuntimeError(f'User resume did not activate the Goal: {goal.get("status")}')
    finally:
        server.close()
    print('PASS: restored the same thread, blocked Goal, and Issue #8 metadata, then resumed it as the user.')
    print('Issue status: the issue is read-only input; the smoke does not update or close it.')
    print('Model turns invoked: 0.')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('phase', choices=['create', 'resume'])
    parser.add_argument('--codex-home', type=Path, required=True)
    parser.add_argument('--workspace', type=Path, required=True)
    parser.add_argument('--metadata', type=Path, required=True)
    parser.add_argument('--issue-json', type=Path)
    parser.add_argument('--model', default='gpt-6-luna')
    args = parser.parse_args()
    if not args.workspace.is_dir():
        raise SystemExit(f'Workspace does not exist: {args.workspace}')
    {'create': create, 'resume': resume}[args.phase](args)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f'ERROR: {error}', file=sys.stderr)
        raise SystemExit(1)
