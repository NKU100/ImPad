"""Promote an adapted profile only after hosted smoke of the exact APK succeeds."""
import argparse
import copy
import json
import os
import subprocess
from pathlib import Path

from scripts.ci.app_policy import policy_for_report, validate_targets
from scripts.ci.codex_task import gh, issue_body, write_issue, publish_outcome, create_outcome_issue
from scripts.ci.discover_latest_wechat import append_github_output


def runtime_result(report, smoke, conclusion, app_id=None):
    if report['status'] != 'STATIC_VERIFIED_PENDING_RUNTIME':
        raise ValueError('Runtime acceptance requires static verification')
    policy = policy_for_report(report, app_id)
    identity = report['identity']
    if identity.get('abi') not in policy.supported_abis:
        raise ValueError('Candidate ABI does not match the registered app policy')
    if identity.get('signerSha256', '').lower() != policy.signer_sha256:
        raise ValueError('Candidate signer does not match the registered app policy')
    result = copy.deepcopy(report)
    result['appId'] = policy.app_id
    if conclusion in {'cancelled', 'skipped'}:
        result['runtimeConclusion'] = conclusion
        result['blockers'] = ['Hosted runtime smoke did not complete; rerun runtime verification for this exact build.']
        return result
    verified = (
        conclusion == 'success'
        and smoke.get('status') == 'RUNTIME_SMOKE_VERIFIED'
        and smoke.get('appId', 'wechat') == policy.app_id
        and smoke.get('packageName') == policy.package_name
        and smoke.get('abi') == identity.get('abi')
        and smoke.get('baseline') == policy.runtime_baseline
        and smoke.get('hookIds') == list(policy.required_hook_ids)
        and (policy.app_id == 'qq' or smoke.get('qrPage') == policy.runtime_qr_page)
        and smoke.get('apkSha256') == identity.get('apkSha256')
    )
    if policy.app_id == 'qq':
        from scripts.ci.apps.qq.runtime_policy import validate_request_evidence
        if smoke.get('status') == 'ACCEPTANCE_UNAVAILABLE':
            result.update(status='STATIC_VERIFIED_PENDING_RUNTIME', runtimeConclusion='acceptance-unavailable',
                          blockers=['QQ request identity observation is unavailable; no runtime promotion.'])
            return result
        verified = verified and smoke.get('acceptanceKind') == 'REQUEST_APP_ID_CONTRAST' and validate_request_evidence(
            smoke.get('requestIdentityEvidence'), identity.get('apkSha256'), smoke.get('moduleApkSha256'), report.get('requestIdentityExpectation'))
    result['status'] = 'RUNTIME_VERIFIED' if verified else 'RUNTIME_REJECTED'
    result['blockers'] = [] if verified else ['Hosted runtime smoke failed or exact-build evidence is missing.']
    if verified:
        result['suggestedProfile']['verificationStatus'] = 'runtime-verified-hosted'
    return result


def close_issue_if_complete(repo, issue, status):
    if status == 'RUNTIME_VERIFIED':
        gh('issue', 'close', str(issue), '--repo', repo)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--app-id', default='wechat')
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--smoke', type=Path, required=True)
    parser.add_argument('--conclusion', required=True)
    parser.add_argument('--pr', required=True)
    parser.add_argument('--head', required=True)
    parser.add_argument('--issue', required=True)
    args = parser.parse_args()
    directory = args.directory
    report = json.loads((directory / 'candidate-report.json').read_text())
    smoke = json.loads(args.smoke.read_text()) if args.smoke.is_file() else {}
    result = runtime_result(report, smoke, args.conclusion, args.app_id)
    repo = os.environ['GITHUB_REPOSITORY']
    run_url = f"https://github.com/{repo}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
    pr = json.loads(gh('pr', 'view', args.pr, '--repo', repo, '--json', 'headRefOid,headRefName,state,body'))
    if pr['headRefOid'] != args.head or pr['state'] != 'OPEN':
        raise ValueError('Adaptation PR changed or closed after the tested build; refusing to promote it')
    if result['status'] == 'RUNTIME_VERIFIED':
        # Read only the manifest from the tested commit; never execute PR scripts with write credentials.
        manifest = policy_for_report(report, args.app_id).targets_path
        subprocess.run(['git', 'fetch', 'origin', args.head], check=True)
        targets = json.loads(subprocess.check_output(['git', 'show', f'{args.head}:{manifest}']))
        validate_targets(targets, args.app_id)
        profile = next(p for p in targets if p['identity'] == report['identity'])
        if profile != report['suggestedProfile']:
            raise ValueError('Tested profile differs from the statically verified report')
        profile['verificationStatus'] = 'runtime-verified-hosted'
        blob = subprocess.check_output(['git', 'hash-object', '-w', '--stdin'],
                                       input=(json.dumps(targets, indent=2) + '\n').encode()).decode().strip()
        env = dict(os.environ, GIT_INDEX_FILE=str(directory.resolve() / 'promotion.index'))
        subprocess.run(['git', 'read-tree', args.head], env=env, check=True)
        subprocess.run(['git', 'update-index', '--cacheinfo', f'100644,{blob},{manifest}'], env=env, check=True)
        tree = subprocess.check_output(['git', 'write-tree'], env=env).decode().strip()
        env.update(GIT_AUTHOR_NAME='NKU100', GIT_COMMITTER_NAME='NKU100',
                   GIT_AUTHOR_EMAIL='21164383+NKU100@users.noreply.github.com',
                   GIT_COMMITTER_EMAIL='21164383+NKU100@users.noreply.github.com')
        commit = subprocess.check_output(['git', 'commit-tree', tree, '-p', args.head, '-m',
                                         'compat: record hosted runtime verification'], env=env).decode().strip()
        subprocess.run(['git', 'push', 'origin', f'{commit}:refs/heads/{pr["headRefName"]}'], check=True)
        body = directory / 'runtime-pr-body.md'
        if args.app_id == 'qq':
            body.write_text(pr['body'] + f'\n\nHosted QQ request identity smoke passed. No account authentication, server acceptance or dual-device coexistence verified. [Evidence]({run_url}). Ready for merge; formal support begins after merge.\n')
        else:
            body.write_text(pr['body'] + f'\n\nHosted smoke passed: injection, Phone & Tablet entry and QR page. No account login. [Evidence]({run_url}). Ready for merge; formal support begins after merge.\n')
        gh('pr', 'edit', args.pr, '--repo', repo, '--body-file', str(body))
        gh('pr', 'ready', args.pr, '--repo', repo)
    elif result['status'] == 'RUNTIME_REJECTED':
        body = directory / 'runtime-failure.md'
        body.write_text(f'Hosted smoke rejected this build. [Diagnostics]({run_url}). The next scheduled run will continue from the saved adaptation checkpoint.\n')
        gh('pr', 'comment', args.pr, '--repo', repo, '--body-file', str(body))
    issue_status = 'WAITING_RUNTIME' if result['status'] == 'STATIC_VERIFIED_PENDING_RUNTIME' else result['status']
    policy = policy_for_report(result, args.app_id)
    checkpoint_artifact = f'{policy.artifact_prefix}-{policy.app_id}-codex-adaptation'
    accepted_head = commit if result['status'] == 'RUNTIME_VERIFIED' else pr['headRefOid']
    if args.issue == '0' and result['status'] == 'RUNTIME_REJECTED':
        args.issue = str(create_outcome_issue(repo, result, 'interrupted', run_url, directory,
                                              checkpoint_run_url=run_url, artifact=checkpoint_artifact))
    if args.issue != '0' and result['status'] in {'RUNTIME_VERIFIED', 'RUNTIME_REJECTED'}:
        outcome = 'verified' if result['status'] == 'RUNTIME_VERIFIED' else 'interrupted'
        publish_outcome(repo, args.issue, result, outcome, run_url, directory,
                        result.get('adaptationSummary'), run_url, checkpoint_artifact, args.pr, accepted_head)
    elif args.issue != '0':
        write_issue(repo, args.issue, issue_body(result, issue_status, run_url, args.pr,
                    run_url, checkpoint_artifact, accepted_head), directory)
    (directory / 'runtime-candidate-report.json').write_text(json.dumps(result, indent=2) + '\n')
    append_github_output(Path(os.environ['GITHUB_OUTPUT']), {'pipeline_status': result['status']})
    with Path(os.environ['GITHUB_STEP_SUMMARY']).open('a') as output:
        output.write(f'## Hosted runtime result\n\nStatus: `{result["status"]}`\n\nPR: {args.pr}\n')


if __name__ == '__main__':
    main()
