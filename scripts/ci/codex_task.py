"""Persist adaptation records and publish only validated adaptation patches."""

import argparse
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

from scripts.ci.adaptation_summary import read_summary, render_result
from scripts.ci.app_policy import get_policy, policy_for_report
from scripts.ci.codex_adaptation import candidate_key, eligible_candidate, patch_tree
from scripts.ci.discover_latest_wechat import append_github_output


ADAPTATION_LABEL = "impad-adaptation"
LEGACY_ADAPTATION_LABEL = "wechatpad-adaptation"
RENAMED_REPOSITORY = "NKU100/ImPad"
LEGACY_REPOSITORY = "NKU100/WeChatPad"


def tracking_markers(key):
    return (f"<!-- {ADAPTATION_LABEL}:{key} -->",
            f"<!-- {LEGACY_ADAPTATION_LABEL}:{key} -->")


def candidate_attempts(issues, key):
    markers = tracking_markers(key)
    return [issue for issue in issues if trusted_attempt(issue)
            and any(marker in (issue.get("body") or "") for marker in markers)]


def issue_candidate_key(issue):
    body = issue.get("body") or ""
    match = re.search(r"<!-- (?:impad-adaptation|wechatpad-adaptation):([^ ]+) -->", body)
    return match[1] if match else ""


def issue_status(issue):
    match = re.search(r"^Status: `([A-Z_]+)`$", issue.get("body") or "", re.MULTILINE)
    return match[1] if match else "ADAPTATION_RUNNING"


def trusted_attempt(issue):
    return (issue.get("user", {}).get("login") == "github-actions[bot]"
            and issue.get("user", {}).get("type") == "Bot"
            and "pull_request" not in issue)


def existing_attempt(issues, key):
    matches = [issue for issue in candidate_attempts(issues, key)
               if any(label.get("name") in {ADAPTATION_LABEL, LEGACY_ADAPTATION_LABEL}
                      for label in issue.get("labels", []))]
    if len(matches) > 1:
        raise ValueError("Multiple trusted adaptation records match this APK key")
    return matches[0] if matches else None


def pending_attempt(issues, app_id):
    prefix = app_id + "-"
    matches = [issue for issue in issues if trusted_attempt(issue)
               and any(label.get("name") in {ADAPTATION_LABEL, LEGACY_ADAPTATION_LABEL}
                       for label in issue.get("labels", []))
               and issue.get("state") == "open"
               and issue_candidate_key(issue).startswith(prefix)
               and issue_status(issue) != "RUNTIME_VERIFIED"]
    if not matches:
        return None
    return min(matches, key=lambda issue: (issue.get("created_at", ""), issue.get("number", 0)))


def list_issues(repo):
    pages = json.loads(gh("api", "--paginate", "--slurp", f"repos/{repo}/issues?state=all&per_page=100"))
    return [issue for page in pages for issue in page]


def owner_comments(comments, owner, cursor=0):
    return sorted(({'id': c['id'], 'body': c['body'], 'url': c.get('html_url', '')}
                   for c in comments if c.get('user', {}).get('login') == owner
                   and c.get('user', {}).get('type') == 'User'
                   and c.get('id', 0) > cursor and (c.get('body') or '').strip()),
                  key=lambda c: c['id'])


def comment_cursor(body):
    match = re.search(r'^Consumed owner comment: `(\d+)`$', body or '', re.MULTILINE)
    return int(match[1]) if match else 0


def read_owner_comments(repo, issue):
    pages = json.loads(gh('api', '--paginate', '--slurp',
                         f"repos/{repo}/issues/{issue['number']}/comments?per_page=100"))
    return owner_comments([c for page in pages for c in page], repo.split('/')[0],
                          comment_cursor(issue.get('body')))


def pending(repo, app_id):
    issues = list_issues(repo)
    active = [issue for issue in issues if trusted_attempt(issue)
              and issue.get('state') == 'open'
              and issue_candidate_key(issue).startswith(app_id + '-')]
    comments_by_issue = {issue['number']: read_owner_comments(repo, issue) for issue in active}
    issue = pending_attempt([issue for issue in active if comments_by_issue[issue['number']]], app_id)
    has_owner_comments = issue is not None
    if issue is None:
        issue = pending_attempt(active, app_id)
    if issue is None:
        result = {"pending_adaptation": "false", "pending_issue": "", "pending_candidate_key": "",
                  "pending_checkpoint_run_id": "", "pending_checkpoint_artifact": ""}
    else:
        run_id, artifact = checkpoint_reference(issue.get("body") or "", repo, app_id)
        result = {"pending_adaptation": str(has_owner_comments).lower(), "pending_issue": str(issue["number"]),
                  "pending_candidate_key": issue_candidate_key(issue),
                  "pending_checkpoint_run_id": run_id or "", "pending_checkpoint_artifact": artifact or ""}
    append_github_output(Path(os.environ["GITHUB_OUTPUT"]), result)
    print(json.dumps(result, sort_keys=True))
    return result


def candidate_cache_settings(report):
    policy = policy_for_report(report)
    from scripts.ci.discover_latest_wechat import validate_official_apk_url
    validate_official_apk_url(report['sourceUrl'], policy.official_apk_prefix)
    digest = hashlib.sha256(report['sourceUrl'].encode('utf-8')).hexdigest()
    return {'cache_dir':f'.cache/{policy.artifact_prefix}-{policy.app_id}-latest/{digest}',
            'cache_key':f'{policy.artifact_prefix}-{policy.app_id}-latest-url-{digest}'}


def select_candidate_report(current_report_path, checkpoint_directory, expected_key, destination):
    current_report_path = Path(current_report_path)
    checkpoint_report_path = Path(checkpoint_directory) / "candidate-report.json"
    candidates = [checkpoint_report_path, current_report_path] if expected_key else [current_report_path]
    for path in candidates:
        if not path.is_file():
            continue
        report = json.loads(path.read_text())
        if not expected_key or candidate_key(report) == expected_key:
            Path(destination).parent.mkdir(parents=True, exist_ok=True)
            Path(destination).write_text(json.dumps(report, indent=2) + "\n")
            return report
    raise ValueError("No verified candidate report matches the active adaptation checkpoint")


def candidate_matches_checkpoint(report_path, expected_key):
    if not expected_key:
        return False
    return candidate_key(json.loads(Path(report_path).read_text())) == expected_key


def action_run_id_from_url(url, repo):
    repositories = [repo]
    if repo.casefold() == RENAMED_REPOSITORY.casefold():
        repositories.append(LEGACY_REPOSITORY)
    for trusted_repo in repositories:
        match = re.fullmatch(
            r"https://github\.com/" + re.escape(trusted_repo) + r"/actions/runs/(\d+)", url
        )
        if match:
            return match[1]
    return None


def checkpoint_reference(body, repo, app_id):
    policy = get_policy(app_id)
    run_match = re.search(r"^Checkpoint run: (\S+)$", body, re.MULTILINE)
    artifact_match = re.search(r"^Checkpoint artifact: `([^`]+)`$", body, re.MULTILINE)
    if '<!-- impad-state' in body and not artifact_match:
        return None, None
    if not run_match:
        run_match = re.search(r"^Run: (\S+)$", body, re.MULTILINE)
    run_id = action_run_id_from_url(run_match[1], repo) if run_match else None
    artifact = artifact_match[1] if artifact_match else f"{policy.artifact_prefix}-{policy.app_id}-codex-adaptation"
    try:
        artifact = validate_checkpoint_artifact(policy.app_id, artifact)
    except ValueError:
        artifact = ''
    if not run_id or not artifact:
        return None, None
    return run_id, artifact


def validate_checkpoint_artifact(app_id, artifact):
    policy = get_policy(app_id)
    expected = f"{policy.artifact_prefix}-{policy.app_id}-codex-adaptation"
    if artifact == expected or re.fullmatch(re.escape(expected) + r'-live-[1-9][0-9]*-[1-9][0-9]*(?:-[a-f0-9]{64}(?:-(?:running|verified|blocked|interrupted))?)?', artifact):
        return artifact
    raise ValueError('Checkpoint artifact is not a trusted artifact for this application')


def adaptation_pr_reference(body, repo):
    repositories = [repo]
    if repo.casefold() == RENAMED_REPOSITORY.casefold():
        repositories.append(LEGACY_REPOSITORY)
    match = re.search(r"^Adaptation PR: (\S+)$", body, re.MULTILINE)
    if not match:
        return ""
    for trusted_repo in repositories:
        if re.fullmatch(r"https://github\.com/" + re.escape(trusted_repo) + r"/pull/\d+", match[1]):
            return match[1]
    return ""


def gh(*args):
    return subprocess.check_output(["gh", *args]).decode().strip()


def issue_body(report, status, run_url, pr_url="", checkpoint_run_url="", checkpoint_artifact="", pr_head="",
               previous_blockers=()):
    identity = report["identity"]
    policy = policy_for_report(report)
    lines = [f"<!-- impad-adaptation:{candidate_key(report)} -->", f"Status: `{status}`", "",
             f"{policy.display_name} {identity['versionName']} ({identity['versionCode']})", f"APK SHA-256: `{identity['apkSha256']}`",
             f"Source: {report['sourceUrl']}", f"Run: {run_url}"]
    if checkpoint_run_url and checkpoint_artifact:
        lines.extend([f"Checkpoint run: {checkpoint_run_url}", f"Checkpoint artifact: `{checkpoint_artifact}`"])
    lines.extend(["", "Model: `gpt-6-luna`; reasoning: `xhigh`; blocked adaptation waits for new owner input or a new APK."])
    for blocker in report.get("blockers", []):
        lines.append(f"Blocker: {blocker}")
    for blocker in previous_blockers:
        if blocker not in report.get("blockers", []):
            lines.append(f"Previous blocker: {blocker}")
    if pr_url:
        lines.extend(["", f"Adaptation PR: {pr_url}"])
        if pr_head:
            lines.append(f"PR head: `{pr_head}`")
        lines.append("Hosted login-screen runtime evidence and merge are required before formal support.")
    if status == "RUNTIME_VERIFIED":
        lines.extend(["", "Static build and hosted runtime verification passed. Adaptation is complete."])
    marker = lines.pop(0)
    machine = "\n".join(lines)
    state = {'RUNTIME_VERIFIED':'verified', 'BLOCKED':'blocked', 'INTERRUPTED':'interrupted', 'ADAPTATION_RUNNING':'running'}.get(status, 'pending')
    visible = render_result(policy.display_name, identity['versionName'], state, run_url,
                            checkpoint=('检查点已保存。' if checkpoint_artifact else ''))
    return visible + "\n" + marker + "\n<!-- impad-state\n" + machine + "\n-->\n"


def visible_issue_body(body, report, run_url):
    if '<!-- impad-state' in body:
        return re.sub(r'<!--.*?-->', '', body, flags=re.DOTALL).strip()
    policy = policy_for_report(report)
    status = 'verified' if issue_status({'body':body}) == 'RUNTIME_VERIFIED' else 'interrupted'
    return render_result(policy.display_name, report['identity']['versionName'], status, run_url).strip()


def write_issue(repo, issue, body, directory):
    path = directory / "issue-body.md"
    context = json.loads((directory / 'resume-context.json').read_text()) if (directory / 'resume-context.json').exists() else {}
    cursor = context.get('consumedCommentId', context.get('issue', {}).get('consumedCommentId', int(os.environ.get('CONSUMED_COMMENT_ID', '0') or '0')))
    if cursor:
        body += f"\n<!--\nConsumed owner comment: `{cursor}`\n-->\n"
    current = gh('api', f'repos/{repo}/issues/{issue}', '--jq', '.body')
    created = re.search(r'<!-- impad-created-run:[0-9]+ -->', current)
    if created and created[0] not in body:
        body += '\n' + created[0] + '\n'
    path.write_text(body)
    return gh("issue", "edit", str(issue), "--repo", repo, "--body-file", str(path))


def create_outcome_issue(repo, report, status, run_url, directory, summary=None,
                         checkpoint_run_url='', artifact=''):
    policy = policy_for_report(report)
    terminal = {'verified':'RUNTIME_VERIFIED', 'blocked':'BLOCKED', 'interrupted':'INTERRUPTED'}[status]
    metadata = issue_body(report, terminal, run_url, checkpoint_run_url=checkpoint_run_url,
                          checkpoint_artifact=artifact)
    visible = render_result(policy.display_name, report['identity']['versionName'], status, run_url, summary,
                            '检查点已保存，可恢复代码和会话。' if artifact else '')
    body = Path(directory) / 'issue-body.md'
    body.write_text(visible + '\n' + metadata[metadata.index('<!-- impad-adaptation'):] +
                    f'\n<!-- impad-created-run:{action_run_id_from_url(run_url, repo)} -->\n')
    url = gh('issue', 'create', '--repo', repo, '--title',
             f"Adapt {policy.display_name} {report['identity']['versionName']} ({candidate_key(report)})",
             '--body-file', str(body), '--label', ADAPTATION_LABEL)
    return int(url.rsplit('/', 1)[1])


def orphan_checkpoint(repo, report):
    policy = policy_for_report(report)
    prefix = f'{policy.artifact_prefix}-{policy.app_id}-codex-adaptation-live-'
    digest = report['identity']['apkSha256'].lower()
    pages = json.loads(gh('api', '--paginate', '--slurp', f'repos/{repo}/actions/artifacts?per_page=100') or '[]')
    candidates = [a for page in pages if isinstance(page, dict) for a in page.get('artifacts', [])
                  if not a.get('expired') and a.get('workflow_run', {}).get('head_branch') == 'main'
                  and re.fullmatch(re.escape(prefix) + r'[1-9][0-9]*-[1-9][0-9]*-' + digest + r'(?:-(?:running|verified|blocked|interrupted))?', a.get('name', ''))]
    if not candidates:
        return None
    return max(candidates, key=lambda a: a['id'])


def claim(repo, report, targets, directory, run_url, app_id=None):
    directory.mkdir(parents=True, exist_ok=True)
    policy = policy_for_report(report, app_id)
    if not eligible_candidate(report, targets, policy.app_id):
        raise ValueError("Only verified, unknown candidate builds may start an adaptation")
    key = candidate_key(report)
    pages = json.loads(gh("api", "--paginate", "--slurp", f"repos/{repo}/issues?state=all&per_page=100"))
    issues = [issue for page in pages for issue in page]
    # Read both marker generations and fail closed if a candidate has duplicate records.
    matches = [i for i in candidate_attempts(issues, key)
               if i.get("state") == "open" or issue_status(i) == "RUNTIME_VERIFIED"]
    if len(matches) > 1:
        raise ValueError("Multiple trusted adaptation records match this APK key; refusing to retry or create another issue")
    previous = matches[0] if matches else None
    gh("label", "create", ADAPTATION_LABEL, "--repo", repo, "--color", "1D76DB", "--description", f"{policy.display_name} adaptation pipeline record", "--force")
    if previous:
        body_text = previous.get("body") or ""
        status_match = re.search(r"^Status: `([A-Z_]+)`$", body_text, re.MULTILINE)
        previous_status = status_match[1] if status_match else "ADAPTATION_RUNNING"
        if previous_status == "RUNTIME_VERIFIED":
            if previous.get("state") != "closed":
                gh("issue", "close", str(previous["number"]), "--repo", repo)
            append_github_output(Path(os.environ["GITHUB_OUTPUT"]), {
                "claimed": "false", "issue": str(previous["number"]),
                "checkpoint_run_id": "", "checkpoint_artifact": "",
                "existing_pr": adaptation_pr_reference(body_text, repo),
            })
            print(f"Candidate adaptation #{previous['number']} is already runtime-verified.")
            return
        gh("issue", "edit", str(previous["number"]), "--repo", repo, "--add-label", ADAPTATION_LABEL)
        checkpoint_run_id, checkpoint_artifact = checkpoint_reference(body_text, repo, policy.app_id)
        checkpoint_run_url = (f"https://github.com/{repo}/actions/runs/{checkpoint_run_id}"
                              if checkpoint_run_id else "")
        pr_url = adaptation_pr_reference(body_text, repo)
        pr_head_match = re.search(r"^PR head: `([a-f0-9]{40})`$", body_text, re.MULTILINE)
        previous_blockers = re.findall(r"^(?:Blocker|Previous blocker): (.+)$", body_text, re.MULTILINE)
        context = {
            "status": previous_status,
            "checkpointRunId": checkpoint_run_id,
            "checkpointArtifact": checkpoint_artifact,
            "existingPr": pr_url,
            "existingPrHead": pr_head_match[1] if pr_head_match else "",
            "previousBlockers": previous_blockers,
            "ownerComments": read_owner_comments(repo, previous),
            "consumedCommentId": comment_cursor(body_text),
            "previousRun": re.search(r"^Run: (\S+)$", body_text, re.MULTILINE)[1]
            if re.search(r"^Run: (\S+)$", body_text, re.MULTILINE) else "",
        }
        (directory / "resume-context.json").write_text(json.dumps(context, indent=2) + "\n")
        if previous.get("state") == "closed":
            gh("issue", "reopen", str(previous["number"]), "--repo", repo)
        metadata = issue_body(report, "ADAPTATION_RUNNING", run_url, pr_url,
                    checkpoint_run_url, checkpoint_artifact, pr_head_match[1] if pr_head_match else "",
                    previous_blockers)
        if '<!-- impad-adaptation' in metadata:
            metadata = visible_issue_body(body_text, report, run_url) + '\n\n' + metadata[metadata.index('<!-- impad-adaptation'):]
        write_issue(repo, previous["number"], metadata, directory)
        outputs = {"claimed": "true", "issue": str(previous["number"]), "checkpoint_run_id": checkpoint_run_id or "",
                   "checkpoint_artifact": checkpoint_artifact or "", "existing_pr": pr_url}
        append_github_output(Path(os.environ["GITHUB_OUTPUT"]), outputs)
        print(f"Resuming adaptation record #{previous['number']} from its latest available checkpoint.")
        return
    orphan = orphan_checkpoint(repo, report)
    if orphan:
        checkpoint_url = f"https://github.com/{repo}/actions/runs/{orphan['workflow_run']['id']}"
        number = create_outcome_issue(repo, report, 'interrupted', run_url, directory,
                                      checkpoint_run_url=checkpoint_url, artifact=orphan['name'])
        append_github_output(Path(os.environ['GITHUB_OUTPUT']), {'claimed':'false', 'issue':str(number),
                            'checkpoint_run_id':str(orphan['workflow_run']['id']), 'checkpoint_artifact':orphan['name'], 'existing_pr':''})
        return
    (directory / 'resume-context.json').write_text(json.dumps({'status':'ADAPTATION_RUNNING'}) + '\n')
    append_github_output(Path(os.environ['GITHUB_OUTPUT']), {'claimed':'true', 'issue':'0',
                        'checkpoint_run_id':'', 'checkpoint_artifact':'', 'existing_pr':''})
    print('Starting a new candidate; an issue will be created only if the run blocks or is interrupted.')


def managed_pr_author(author, owner):
    if not isinstance(author, dict):
        return False
    if author.get('is_bot') is True:
        return author.get('login') in {'github-actions[bot]', 'app/github-actions'}
    return author.get('login') == owner


PR_FIELDS = 'url,headRefName,headRefOid,state,baseRefName,author,body,isDraft,isCrossRepository'


def validate_managed_pr(pr, branch_prefix, repo, issue, expected_head='', allow_unlinked=False):
    branch = pr.get('headRefName', '')
    if not (branch == branch_prefix or re.fullmatch(re.escape(branch_prefix) + r'-run-[0-9]+', branch)):
        raise ValueError("Existing adaptation PR does not use the candidate's managed branch")
    links = re.findall(r'\btracks #([0-9]+)\b', pr.get('body') or '')
    if (pr.get('state') != 'OPEN' or pr.get('baseRefName') != 'main'
            or pr.get('isCrossRepository')
            or not managed_pr_author(pr.get('author'), repo.split('/')[0])
            or (links != [str(issue)] and not (allow_unlinked and not links))):
        raise ValueError('Existing adaptation PR is no longer a matching open managed PR')
    if expected_head and pr.get('headRefOid') != expected_head:
        raise ValueError('Existing adaptation PR changed after the last checkpoint')


def recover_publication(repo, checkout, directory, resume, report, branch, issue, run_url, body, title):
    prs = json.loads(gh('pr', 'list', '--repo', repo, '--head', branch, '--state', 'all',
                       '--json', PR_FIELDS))
    if len(prs) > 1:
        raise ValueError('Multiple adaptation PRs use the candidate branch')
    previous_pr = prs[0] if prs else None
    if previous_pr:
        validate_managed_pr(previous_pr, branch, repo, issue, allow_unlinked=True)
        if previous_pr['headRefName'] != branch:
            raise ValueError('Recovered adaptation PR has a different candidate branch')
        if not adaptation_pr_reference('Adaptation PR: ' + previous_pr.get('url', ''), repo):
            raise ValueError('Recovered adaptation PR belongs to a different repository')
        if not re.findall(r'\btracks #([0-9]+)\b', previous_pr.get('body') or ''):
            checkpoint_runs = {resume.get('checkpointRunId', '')}
            checkpoint_runs.update(checkpoint.get('runId', '')
                                   for checkpoint in resume.get('publicationCheckpoints', []))
            if not any(re.fullmatch(r'[0-9]+', str(checkpoint_run))
                       and f'[Adaptation run](https://github.com/{repo}/actions/runs/{checkpoint_run})'
                       in previous_pr.get('body', '') for checkpoint_run in checkpoint_runs):
                raise ValueError('Unlinked adaptation PR does not match the checkpoint run')
    ref = 'refs/heads/' + branch
    refs = subprocess.check_output(['git', 'ls-remote', '--refs', 'origin', ref], cwd=checkout).decode().splitlines()
    if not refs:
        if previous_pr:
            raise ValueError('Recovered adaptation PR branch no longer exists')
        return '', None
    if len(refs) != 1 or refs[0].split()[1] != ref:
        raise ValueError('Candidate branch lookup is ambiguous')
    remote_head = refs[0].split()[0]
    if previous_pr and previous_pr['headRefOid'] != remote_head:
        raise ValueError('Recovered adaptation PR changed after branch lookup')
    subprocess.run(['git', 'fetch', '--quiet', '--no-tags', 'origin', ref], cwd=checkout, check=True)
    fetched_head = subprocess.check_output(['git', 'rev-parse', 'FETCH_HEAD'], cwd=checkout).decode().strip()
    if fetched_head != remote_head:
        raise ValueError('Candidate branch changed during recovery')
    checkpoints = resume.get('publicationCheckpoints') or [{
        'base': json.loads((directory / 'state.json').read_text())['base'],
        'tree': patch_tree(checkout, 'HEAD', directory / 'adaptation.patch'),
    }]
    commit = subprocess.check_output(['git', 'show', '-s', '--format=%P%n%T%n%an%n%ae%n%cn%n%ce%n%s',
                                      remote_head], cwd=checkout).decode().splitlines()
    expected_author = ['NKU100', '21164383+NKU100@users.noreply.github.com', 'NKU100',
                       '21164383+NKU100@users.noreply.github.com', title]
    if (commit[2:] != expected_author
            or commit[:2] not in [[checkpoint['base'], checkpoint['tree']] for checkpoint in checkpoints]):
        raise ValueError('Candidate branch does not match the published checkpoint commit')
    if previous_pr:
        url = previous_pr['url']
        if not previous_pr.get('isDraft'):
            gh('pr', 'ready', url, '--repo', repo, '--undo')
        gh('pr', 'edit', url, '--repo', repo, '--body-file', str(body))
    else:
        url = gh('pr', 'create', '--repo', repo, '--base', 'main', '--head', branch, '--draft',
                 '--title', title, '--body-file', str(body))
    previous_pr = json.loads(gh('pr', 'view', url, '--repo', repo, '--json', PR_FIELDS))
    validate_managed_pr(previous_pr, branch, repo, issue, remote_head)
    resume.update(existingPr=url, existingPrHead=remote_head)
    (directory / 'resume-context.json').write_text(json.dumps(resume, indent=2) + '\n')
    if issue:
        current = gh('api', f'repos/{repo}/issues/{issue}', '--jq', '.body')
        checkpoint_url = f"https://github.com/{repo}/actions/runs/{resume['checkpointRunId']}" if resume.get('checkpointRunId') else ''
        metadata = issue_body(report, 'INTERRUPTED', run_url, url, checkpoint_url,
                              resume.get('checkpointArtifact', ''), remote_head)
        write_issue(repo, issue, visible_issue_body(current, report, run_url) + '\n\n' +
                    metadata[metadata.index('<!-- impad-adaptation'):], directory)
    return url, previous_pr


def publish(repo, directory, issue, run_url, app_id=None):
    state = json.loads((directory / "state.json").read_text())
    report = json.loads((directory / "candidate-report.json").read_text())
    policy = policy_for_report(report, app_id or state.get("appId"))
    if state.get("appId", "wechat") != policy.app_id:
        raise ValueError("Adaptation state appId does not match the candidate report")
    if report["status"] != "STATIC_VERIFIED_PENDING_RUNTIME":
        raise ValueError("Draft PR requires successful candidate and regression checks")
    key = candidate_key(report)
    run_id = re.fullmatch(r"https://github\.com/" + re.escape(repo) + r"/actions/runs/(\d+)", run_url)
    if run_id is None:
        raise ValueError("Publication requires a valid workflow run URL")
    resume = json.loads((directory / "resume-context.json").read_text()) if (directory / "resume-context.json").is_file() else {}
    existing_pr = resume.get("existingPr", "")
    expected_pr_head = resume.get("existingPrHead", "")
    branch_prefix = f"adapt/{policy.app_id}-" + key
    branch = branch_prefix
    previous_pr = None
    if existing_pr:
        previous_pr = json.loads(gh("pr", "view", existing_pr, "--repo", repo, "--json",
                                    PR_FIELDS))
        validate_managed_pr(previous_pr, branch_prefix, repo, issue, expected_pr_head)
        branch = previous_pr['headRefName']
    checkout = Path(os.environ["GITHUB_WORKSPACE"])
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=checkout).decode().strip()
    current_main = gh("api", f"repos/{repo}/git/ref/heads/main", "--jq", ".object.sha")
    if head != state["base"] or current_main != head:
        raise ValueError("Main changed during adaptation; manual reconciliation is required")
    body = directory / "pr-body.md"
    versions = ", ".join(report["checkedVersions"])
    body.write_text(f"Adds the verified {policy.display_name} {report['identity']['versionName']} build as a static-verified profile.\n\n"
                    f"The shared checker passed for: {versions}. Shared-core and module tests and the debug APK build passed.\n\n"
                    f"Hosted runtime smoke will verify {policy.display_name} hook installation and the policy-specific login page before this PR is ready.\n\n"
                    f"[Adaptation run]({run_url})" + (f"; tracks #{issue}.\n" if issue else ".\n"))
    title = f"feat: support {policy.display_name} {report['identity']['versionName']}"
    if not existing_pr:
        existing_pr, previous_pr = recover_publication(repo, checkout, directory, resume, report,
                                                      branch, issue, run_url, body, title)
    if previous_pr and not previous_pr.get('isDraft'):
        gh('pr', 'ready', existing_pr, '--repo', repo, '--undo')
    subprocess.run(["git", "switch", "-c", branch], cwd=checkout, check=True)
    subprocess.run(["git", "apply", "--index", str(directory / "adaptation.patch")], cwd=checkout, check=True)
    subprocess.run(["git", "config", "user.name", "NKU100"], cwd=checkout, check=True)
    subprocess.run(["git", "config", "user.email", "21164383+NKU100@users.noreply.github.com"], cwd=checkout, check=True)
    subprocess.run(["git", "commit", "-m", f"feat: support {policy.display_name} {report['identity']['versionName']}"], cwd=checkout, check=True)
    if previous_pr:
        lease = f"--force-with-lease=refs/heads/{branch}:{previous_pr['headRefOid']}"
        subprocess.run(["git", "push", lease, "origin", "HEAD:refs/heads/" + branch], cwd=checkout, check=True)
    else:
        subprocess.run(["git", "push", "origin", "HEAD:refs/heads/" + branch], cwd=checkout, check=True)
    if previous_pr:
        url = existing_pr
        gh("pr", "edit", url, "--repo", repo, "--title", title, "--body-file", str(body))
    else:
        url = gh("pr", "create", "--repo", repo, "--base", "main", "--head", branch, "--draft",
                 "--title", title, "--body-file", str(body))
    head_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=checkout).decode().strip()
    checkpoint_run_id = resume.get("checkpointRunId", "")
    checkpoint_artifact = resume.get("checkpointArtifact", "")
    checkpoint_url = f"https://github.com/{repo}/actions/runs/{checkpoint_run_id}" if checkpoint_run_id else ""
    if issue:
        current = gh('api', f'repos/{repo}/issues/{issue}', '--jq', '.body')
        metadata = issue_body(report, 'WAITING_RUNTIME', run_url, url, checkpoint_url, checkpoint_artifact, head_sha)
        write_issue(repo, issue, visible_issue_body(current, report, run_url) + '\n\n' + metadata[metadata.index('<!-- impad-adaptation'):], directory)
    append_github_output(Path(os.environ["GITHUB_OUTPUT"]), {"pipeline_status": "WAITING_RUNTIME", "pr_url": url, "head_sha": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=checkout).decode().strip()})
    print(url)


def publish_outcome(repo, issue, report, status, run_url, directory, summary=None,
                    checkpoint_run_url='', artifact='', pr_url='', pr_head=''):
    directory = Path(directory)
    policy = policy_for_report(report)
    run_id = action_run_id_from_url(run_url, repo)
    if run_id is None:
        raise ValueError('Outcome requires a trusted CI run URL')
    marker = f'<!-- impad-result:{run_id} -->'
    current = gh('api', f'repos/{repo}/issues/{issue}', '--jq', '.body')
    visible = render_result(policy.display_name, report['identity']['versionName'], status,
                            run_url, summary, '检查点已保存，可恢复代码和会话。' if artifact else '')
    terminal = {'verified':'RUNTIME_VERIFIED', 'blocked':'BLOCKED', 'interrupted':'INTERRUPTED'}[status]
    metadata = issue_body(report, terminal, run_url, pr_url, checkpoint_run_url, artifact, pr_head)
    hidden = metadata[metadata.index('<!-- impad-adaptation'):]
    if f'<!-- impad-created-run:{run_id} -->' in current:
        write_issue(repo, issue, visible + '\n' + marker + '\n' + hidden, directory)
    else:
        pages = json.loads(gh('api', '--paginate', '--slurp', f'repos/{repo}/issues/{issue}/comments?per_page=100'))
        comments = [c for page in pages for c in page if c.get('user', {}).get('login') == 'github-actions[bot]' and marker in (c.get('body') or '')]
        if len(comments) > 1:
            raise ValueError('Multiple outcome comments exist for this CI run')
        path = directory / 'attempt-result.md'
        path.write_text(visible + '\n' + marker + '\n')
        if comments:
            gh('api', '--method', 'PATCH', f"repos/{repo}/issues/comments/{comments[0]['id']}", '-f', 'body=' + path.read_text())
        else:
            gh('issue', 'comment', str(issue), '--repo', repo, '--body-file', str(path))
        previous_visible = visible_issue_body(current, report, run_url)
        write_issue(repo, issue, previous_visible + '\n\n' + hidden, directory)
    if status == 'verified':
        gh('issue', 'close', str(issue), '--repo', repo)


def record_checkpoint(repo, report_path, directory, issue, run_url, app_id=None,
                      artifact_name=None, live=False):
    from scripts.ci.adaptation_checkpoint import SCHEMA_VERSION
    directory = Path(directory)
    policy = get_policy(app_id)
    report_file = directory / "candidate-report.json"
    report = json.loads((report_file if report_file.is_file() else report_path).read_text())
    policy_for_report(report, policy.app_id)
    manifest = json.loads((directory / "checkpoint.json").read_text())
    if manifest.get("schemaVersion") != SCHEMA_VERSION or manifest.get("candidateKey") != candidate_key(report):
        raise ValueError("Checkpoint manifest does not match the claimed candidate")
    expected_artifact = (validate_checkpoint_artifact(policy.app_id, artifact_name)
                         if artifact_name else
                         f"{policy.artifact_prefix}-{policy.app_id}-codex-adaptation")
    if action_run_id_from_url(run_url, repo) is None:
        raise ValueError("Checkpoint pointer must refer to a run in this repository")
    context_path = directory / 'resume-context.json'
    context = json.loads(context_path.read_text()) if context_path.exists() else {}
    comments = context.get('ownerComments', context.get('issue', {}).get('ownerComments', []))
    diagnostics_path = directory / 'goal-diagnostics.json'
    diagnostics = json.loads(diagnostics_path.read_text()) if diagnostics_path.exists() else {}
    if comments and diagnostics.get('inputDelivered'):
        context['consumedCommentId'] = max(c['id'] for c in comments)
        context_path.write_text(json.dumps(context, indent=2) + '\n')
    if not issue:
        outcome = diagnostics.get('status', 'interrupted')
        if live or (outcome == 'verified' and report.get('status') == 'STATIC_VERIFIED_PENDING_RUNTIME'):
            append_github_output(Path(os.environ['GITHUB_OUTPUT']), {'checkpoint_saved':'true', 'issue':'0', 'consumed_comment_id':str(context.get('consumedCommentId', 0))})
            return
        outcome = 'blocked' if outcome == 'blocked' else 'interrupted'
        summary = read_summary(directory / 'checkout/work/analysis/adaptation-summary.json')
        issue = create_outcome_issue(repo, report, outcome, run_url, directory, summary, run_url, expected_artifact)
    append_github_output(Path(os.environ['GITHUB_OUTPUT']), {'issue':str(issue)})
    current_body = gh("api", f"repos/{repo}/issues/{issue}", "--jq", ".body")
    pr_url = adaptation_pr_reference(current_body, repo)
    pr_head_match = re.search(r"^PR head: `([a-f0-9]{40})`$", current_body, re.MULTILINE)
    status = ("WAITING_RUNTIME" if manifest.get("stage") == "static-verified-pending-runtime"
              else "ADAPTATION_RUNNING")
    if live:
        # Preserve the visible result; only machine metadata moves at turn boundaries.
        metadata = issue_body(report, status, run_url, pr_url, run_url,
                              expected_artifact, pr_head_match[1] if pr_head_match else "")
        hidden = metadata[metadata.index('<!-- impad-adaptation'):]
        visible = visible_issue_body(current_body, report, run_url)
        write_issue(repo, issue, visible + '\n\n' + hidden, directory)
    else:
        outcome = diagnostics.get('status', 'interrupted')
        if (outcome == 'verified' and pr_url
                and report.get('status') == 'STATIC_VERIFIED_PENDING_RUNTIME'):
            metadata = issue_body(report, 'WAITING_RUNTIME', run_url, pr_url, run_url, expected_artifact, pr_head_match[1] if pr_head_match else '')
            visible_body = visible_issue_body(current_body, report, run_url)
            write_issue(repo, issue, visible_body + '\n\n' + metadata[metadata.index('<!-- impad-adaptation'):], directory)
            append_github_output(Path(os.environ['GITHUB_OUTPUT']), {'checkpoint_saved':'true', 'consumed_comment_id':str(context.get('consumedCommentId', 0))})
            return
        if outcome != 'blocked':
            outcome = 'interrupted'
        summary = read_summary(directory / 'checkout/work/analysis/adaptation-summary.json')
        publish_outcome(repo, issue, report, outcome, run_url, directory, summary,
                        run_url, expected_artifact, pr_url, pr_head_match[1] if pr_head_match else '')
    append_github_output(Path(os.environ["GITHUB_OUTPUT"]), {"checkpoint_saved": "true", "consumed_comment_id": str(context.get("consumedCommentId", 0))})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=["pending", "select-report", "claim", "publish", "fail", "checkpoint", "outcome"])
    parser.add_argument("--report", type=Path)
    parser.add_argument("--targets", type=Path)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--checkpoint-directory", type=Path)
    parser.add_argument("--candidate-key")
    parser.add_argument("--output-report", type=Path)
    parser.add_argument("--app-id", default="wechat")
    parser.add_argument("--issue", type=int)
    parser.add_argument("--artifact-name")
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    policy = get_policy(args.app_id)
    repo = os.environ["GITHUB_REPOSITORY"]
    run_url = f"https://github.com/{repo}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
    if args.stage == "pending":
        pending(repo, policy.app_id)
    elif args.stage == "select-report":
        report = select_candidate_report(args.report, args.checkpoint_directory or args.directory,
                                         args.candidate_key or "", args.output_report)
        append_github_output(Path(os.environ['GITHUB_OUTPUT']), candidate_cache_settings(report))
    elif args.stage == "claim":
        claim(repo, json.loads(args.report.read_text()), json.loads(args.targets.read_text()), args.directory, run_url, app_id=policy.app_id)
    elif args.stage == "publish":
        state = json.loads((args.directory / "state.json").read_text())
        policy = get_policy(state.get("appId"))
        publish(repo, args.directory, args.issue, run_url, policy.app_id)
    elif args.stage == 'outcome':
        report_file = args.directory / 'candidate-report.json'
        report = json.loads((report_file if report_file.is_file() else args.report).read_text())
        if not args.issue:
            number = create_outcome_issue(repo, report, 'interrupted', run_url, args.directory)
            append_github_output(Path(os.environ['GITHUB_OUTPUT']), {'issue':str(number)})
            return
        current = gh('api', f'repos/{repo}/issues/{args.issue}', '--jq', '.body')
        checkpoint_run_id, artifact = checkpoint_reference(current, repo, policy.app_id)
        publish_outcome(repo, args.issue, report, 'interrupted', run_url, args.directory,
                        checkpoint_run_url=f'https://github.com/{repo}/actions/runs/{checkpoint_run_id}' if checkpoint_run_id else '',
                        artifact=artifact or '', pr_url=adaptation_pr_reference(current, repo))
    elif args.stage == "checkpoint":
        record_checkpoint(repo, args.report, args.directory, args.issue, run_url, policy.app_id,
                          args.artifact_name, args.live)
    else:
        report = json.loads(args.report.read_text())
        policy_for_report(report, policy.app_id)
        report.update(status="NEEDS_HOOK_REVIEW", suggestedProfile=None,
                      blockers=["This run did not finish the adaptation. The next scheduled run will resume from the newest available checkpoint."])
        (args.directory / "candidate-report.json").write_text(json.dumps(report, indent=2) + "\n")
        resume = json.loads((args.directory / "resume-context.json").read_text()) if (args.directory / "resume-context.json").is_file() else {}
        checkpoint_run_id = resume.get("checkpointRunId", "")
        checkpoint_artifact = resume.get("checkpointArtifact", "")
        checkpoint_url = f"https://github.com/{repo}/actions/runs/{checkpoint_run_id}" if checkpoint_run_id else ""
        if args.issue:
            write_issue(repo, args.issue, issue_body(report, "ADAPTATION_RUNNING", run_url,
                        resume.get("existingPr", ""), checkpoint_url, checkpoint_artifact,
                        resume.get("existingPrHead", ""), resume.get("previousBlockers", [])), args.directory)
        append_github_output(Path(os.environ["GITHUB_OUTPUT"]), {"pipeline_status": "ADAPTATION_RUNNING"})


if __name__ == "__main__":
    main()
