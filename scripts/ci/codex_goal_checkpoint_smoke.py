"""Real-model synthetic Goal probe using the production checkpoint upload barrier."""
import argparse
import json
import os
import signal
import subprocess
from pathlib import Path
from scripts.ci.codex_goal import AppServer, run_goal, persist_turn
from scripts.ci.adaptation_checkpoint import restore_checkpoint


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('stage', choices=['model'])
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--prompt', type=Path, required=True)
    args = parser.parse_args()
    directory = args.directory
    workspace = directory / 'checkout'
    workspace.mkdir(parents=True, exist_ok=True)
    targets = json.loads((Path(os.environ['GITHUB_WORKSPACE']) / 'compatibility/wecom/targets.json').read_text())
    report = {'appId':'wecom', 'identity':targets[0]['identity'], 'sourceUrl':targets[0]['sourceUrl'], 'status':'NEEDS_HOOK_REVIEW'}
    target = workspace / 'compatibility/wecom/targets.json'
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('{"answer":0}\n')
    (workspace / '.gitignore').write_text('work/\n')
    for command in (['git','init','-q'], ['git','add','.'], ['git','-c','user.name=NKU100', '-c','user.email=21164383+NKU100@users.noreply.github.com', 'commit','-qm','test: initialize synthetic checkpoint fixture']):
        subprocess.run(command, cwd=workspace, check=True, stdout=subprocess.DEVNULL)
    base = subprocess.check_output(['git','rev-parse','HEAD'], cwd=workspace, text=True).strip()
    state = {'appId':'wecom', 'report':report, 'base':base, 'agent_base':base, 'worktree':str(workspace)}
    (directory / 'state.json').write_text(json.dumps(state))
    (directory / 'candidate-report.json').write_text(json.dumps(report))
    analysis = workspace / 'work/analysis'
    analysis.mkdir(parents=True)
    (analysis / 'candidate-report.json').write_text(json.dumps(report))
    apk_dir = workspace / 'work/apks';apk_dir.mkdir(parents=True)
    (apk_dir / ('candidate-' + report['identity']['apkSha256'] + '.apk')).write_text('Synthetic fixture; this probe does not verify Android compatibility.\n')
    context = None
    restore = os.environ.get('SMOKE_RESTORE_DIRECTORY')
    if restore:
        snapshots = sorted(Path(restore).rglob('checkpoint.json'), key=lambda p:int(p.parent.name))
        if not snapshots:
            raise RuntimeError('Missing uploaded turn checkpoint')
        restore_checkpoint(directory, snapshots[-1].parent)
        context = json.loads((directory / 'resume-context.json').read_text())
    env = {key:value for key,value in os.environ.items() if key in {'PATH','HOME','LANG','LC_ALL','CODEX_HOME','SSL_CERT_FILE','TMPDIR'}}
    from scripts.ci import codex_adaptation
    codex_adaptation.REASONING = 'low'
    client = AppServer(directory, env)
    interrupted = os.environ.get('SMOKE_INTERRUPT_AFTER_TURN') == 'true'
    checks = []
    def verify(_):
        answer = json.loads(target.read_text())['answer']
        checks.append(answer)
        (directory/'smoke-acceptance.json').write_text(json.dumps({'answers':checks}))
        if answer != 42:
            raise ValueError('Independent fixture acceptance requires answer=42. Fix the JSON value and self-test it.')
    def checkpoint(root, server, sequence):
        persist_turn(root, server, sequence)
        if interrupted:
            os.kill(os.getpid(), signal.SIGKILL)
    objective = ('Synthetic CI Goal only: edit compatibility/wecom/targets.json. '
                 'No APK analysis, Android builds, external services or commits are needed. '
                 'Before complete or blocked, write work/analysis/adaptation-summary.json with string-array fields completed, blockers, nextActions, questions. '
                 'Do your own tests, then mark the Goal complete; independent acceptance may return additional requirements.')
    prompt = ('Resume the synthetic checkpoint. Set answer=41, self-test JSON parsing, then complete the Goal. '
              'Independent acceptance will provide its real acceptance requirement. Do not guess it in advance.')
    try:
        result = run_goal(client, directory, prompt, context, verifier=verify,
                          checkpoint=checkpoint, objective_override=objective)
        if result != 'verified' or checks != [41,42]:
            raise RuntimeError(f'Unexpected continuation result: {result}, {checks}')
        if context and client.thread_id != context['nativeThreadId']:
            raise RuntimeError('Recovery changed native thread identity')
        (directory/'smoke-result.json').write_text(json.dumps({'status':'VERIFIED', 'threadId':client.thread_id,
            'nativeResume':bool(context), 'acceptanceAnswers':checks}))
    finally:
        client.close()

if __name__ == '__main__':
    main()
