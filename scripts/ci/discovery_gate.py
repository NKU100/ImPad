"""Skip expensive compatibility jobs only for an exact, already supported APK."""
import argparse
import json
from pathlib import Path

from scripts.ci.app_policy import get_policy
from scripts.ci.discover_latest_wechat import append_github_output


def discovery_decision(targets, source_url, apk_sha256, force_recheck, pending_candidate_key=""):
    matches = [target for target in targets if target.get('sourceUrl') == source_url
               and target['identity']['apkSha256'].lower() == apk_sha256.lower()]
    if pending_candidate_key.lower().endswith('-' + apk_sha256.lower()):
        return 'WAITING_FOR_INPUT'
    if force_recheck:
        return 'FORCE_RECHECK'
    if len(matches) > 1:
        raise ValueError('Multiple profiles register the same source APK identity')
    if matches and matches[0].get('verificationStatus') in {'runtime-verified-local', 'runtime-verified-hosted'}:
        return 'SKIP_SUPPORTED_BUILD'
    return 'ANALYZE_CANDIDATE'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--app-id', default='wechat')
    parser.add_argument('--pending-candidate-key', default='')
    parser.add_argument('--targets', type=Path, required=True)
    parser.add_argument('--source-url', required=True)
    parser.add_argument('--apk-sha256', required=True)
    parser.add_argument('--force-recheck', choices=['true', 'false'], default='false')
    parser.add_argument('--github-output', type=Path, required=True)
    parser.add_argument('--summary', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    policy = get_policy(args.app_id)
    decision = discovery_decision(json.loads(args.targets.read_text()), args.source_url,
                                  args.apk_sha256, args.force_recheck == 'true', args.pending_candidate_key)
    append_github_output(args.github_output, {'skip_heavy': str(decision in {'SKIP_SUPPORTED_BUILD', 'WAITING_FOR_INPUT'}).lower()})
    args.report.write_text(json.dumps({'appId': policy.app_id, 'decision': decision, 'sourceUrl': args.source_url,
                                      'apkSha256': args.apk_sha256,
                                      'checkedVersions': [], 'freshRuntimeVerification': False}, indent=2) + '\n')
    with args.summary.open('a') as summary:
        summary.write(f'## Daily discovery\n\nDecision: `{decision}`\n\n')
        if decision == 'SKIP_SUPPORTED_BUILD':
            summary.write('The exact APK is already runtime-verified on main. No static matrix, build, Codex or AVD run was started.\n')


if __name__ == '__main__':
    main()
