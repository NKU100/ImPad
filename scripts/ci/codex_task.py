"""Persist adaptation records and publish only validated adaptation patches."""

import argparse
import json
import os
import re
import subprocess
from pathlib import Path

from scripts.ci.app_policy import get_policy, policy_for_report
from scripts.ci.codex_adaptation import candidate_key, eligible_candidate
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
    actionable = [issue for issue in issues if trusted_attempt(issue)
                  and issue.get('state') == 'open'
                  and issue_candidate_key(issue).startswith(app_id + '-')
                  and read_owner_comments(repo, issue)]
    issue = pending_attempt(actionable, app_id)
    if issue is None:
        result = {"pending_adaptation": "false", "pending_issue": "", "pending_candidate_key": "",
                  "pending_checkpoint_run_id": "", "pending_checkpoint_artifact": ""}
    else:
        run_id, artifact = checkpoint_reference(issue.get("body") or "", repo, app_id)
        result = {"pending_adaptation": "true", "pending_issue": str(issue["number"]),
                  "pending_candidate_key": issue_candidate_key(issue),
                  "pending_checkpoint_run_id": run_id or "", "pending_checkpoint_artifact": artifact or ""}
    append_github_output(Path(os.environ["GITHUB_OUTPUT"]), result)
    print(json.dumps(result, sort_keys=True))
    return result


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
    expected_artifact = f"{policy.artifact_prefix}-{policy.app_id}-codex-adaptation"
    run_match = re.search(r"^Checkpoint run: (\S+)$", body, re.MULTILINE)
    artifact_match = re.search(r"^Checkpoint artifact: `([^`]+)`$", body, re.MULTILINE)
    if not run_match:
        run_match = re.search(r"^Run: (\S+)$", body, re.MULTILINE)
    run_id = action_run_id_from_url(run_match[1], repo) if run_match else None
    artifact = artifact_match[1] if artifact_match else expected_artifact
    if not run_id or artifact != expected_artifact:
        return None, None
    return run_id, artifact


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
    lines.extend(["", "Model: `gpt-6-luna`; reasoning: `xhigh`; unfinished adaptation resumes automatically from its latest checkpoint."])
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
    return "\n".join(lines) + "\n"


def write_issue(repo, issue, body, directory):
    path = directory / "issue-body.md"
    context = json.loads((directory / 'resume-context.json').read_text()) if (directory / 'resume-context.json').exists() else {}
    cursor = context.get('consumedCommentId', context.get('issue', {}).get('consumedCommentId', int(os.environ.get('CONSUMED_COMMENT_ID', '0') or '0')))
    if cursor:
        body += f"\nConsumed owner comment: `{cursor}`\n"
    path.write_text(body)
    return gh("issue", "edit", str(issue), "--repo", repo, "--body-file", str(path))


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
        write_issue(repo, previous["number"], issue_body(report, "ADAPTATION_RUNNING", run_url, pr_url,
                    checkpoint_run_url, checkpoint_artifact, pr_head_match[1] if pr_head_match else "",
                    previous_blockers), directory)
        outputs = {"claimed": "true", "issue": str(previous["number"]), "checkpoint_run_id": checkpoint_run_id or "",
                   "checkpoint_artifact": checkpoint_artifact or "", "existing_pr": pr_url}
        append_github_output(Path(os.environ["GITHUB_OUTPUT"]), outputs)
        print(f"Resuming adaptation record #{previous['number']} from its latest available checkpoint.")
        return
    body = directory / "issue-body.md"
    body.write_text(issue_body(report, "ADAPTATION_RUNNING", run_url))
    url = gh("issue", "create", "--repo", repo, "--title", f"Adapt {policy.display_name} {report['identity']['versionName']} ({key})", "--body-file", str(body), "--label", ADAPTATION_LABEL)
    number = int(url.rsplit("/", 1)[1])
    (directory / "resume-context.json").write_text(json.dumps({"status": "ADAPTATION_RUNNING"}, indent=2) + "\n")
    append_github_output(Path(os.environ["GITHUB_OUTPUT"]), {"claimed": "true", "issue": str(number),
                          "checkpoint_run_id": "", "checkpoint_artifact": "", "existing_pr": ""})
    print(f"Claimed one adaptation attempt: #{number}")


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
                                    "headRefName,headRefOid,state,baseRefName,author,body"))
        previous_branch = previous_pr.get("headRefName", "")
        if not (previous_branch == branch_prefix or previous_branch.startswith(branch_prefix + "-run-")):
            raise ValueError("Existing adaptation PR does not use the candidate's managed branch")
        if (previous_pr.get("state") != "OPEN" or previous_pr.get("baseRefName") != "main"
                or previous_pr.get("author", {}).get("login") not in {"github-actions[bot]", "NKU100"}
                or f"tracks #{issue}" not in previous_pr.get("body", "")):
            raise ValueError("Existing adaptation PR is no longer a matching open managed PR")
        if expected_pr_head and previous_pr.get("headRefOid") != expected_pr_head:
            raise ValueError("Existing adaptation PR changed after the last checkpoint")
        branch = previous_branch
    checkout = Path(os.environ["GITHUB_WORKSPACE"])
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=checkout).decode().strip()
    current_main = gh("api", f"repos/{repo}/git/ref/heads/main", "--jq", ".object.sha")
    if head != state["base"] or current_main != head:
        raise ValueError("Main changed during adaptation; manual reconciliation is required")
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
    body = directory / "pr-body.md"
    versions = ", ".join(report["checkedVersions"])
    body.write_text(f"Adds the verified {policy.display_name} {report['identity']['versionName']} build as a static-verified profile.\n\n"
                    f"The shared checker passed for: {versions}. Shared-core and module tests and the debug APK build passed.\n\n"
                    f"Hosted runtime smoke will verify {policy.display_name} hook installation and the policy-specific login page before this PR is ready.\n\n"
                    f"[Adaptation run]({run_url}); tracks #{issue}.\n")
    title = f"feat: support {policy.display_name} {report['identity']['versionName']}"
    if previous_pr:
        url = existing_pr
        gh("pr", "edit", url, "--repo", repo, "--title", title, "--body-file", str(body), "--draft")
    else:
        url = gh("pr", "create", "--repo", repo, "--base", "main", "--head", branch, "--draft",
                 "--title", title, "--body-file", str(body))
    head_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=checkout).decode().strip()
    checkpoint_run_id = resume.get("checkpointRunId", "")
    checkpoint_artifact = resume.get("checkpointArtifact", "")
    checkpoint_url = f"https://github.com/{repo}/actions/runs/{checkpoint_run_id}" if checkpoint_run_id else ""
    write_issue(repo, issue, issue_body(report, "WAITING_RUNTIME", run_url, url,
                checkpoint_url, checkpoint_artifact, head_sha), directory)
    append_github_output(Path(os.environ["GITHUB_OUTPUT"]), {"pipeline_status": "WAITING_RUNTIME", "pr_url": url, "head_sha": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=checkout).decode().strip()})
    print(url)


def record_checkpoint(repo, report_path, directory, issue, run_url, app_id=None):
    from scripts.ci.adaptation_checkpoint import SCHEMA_VERSION
    directory = Path(directory)
    policy = get_policy(app_id)
    report_file = directory / "candidate-report.json"
    report = json.loads((report_file if report_file.is_file() else report_path).read_text())
    policy_for_report(report, policy.app_id)
    manifest = json.loads((directory / "checkpoint.json").read_text())
    if manifest.get("schemaVersion") != SCHEMA_VERSION or manifest.get("candidateKey") != candidate_key(report):
        raise ValueError("Checkpoint manifest does not match the claimed candidate")
    expected_artifact = f"{policy.artifact_prefix}-{policy.app_id}-codex-adaptation"
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
    current_body = gh("api", f"repos/{repo}/issues/{issue}", "--jq", ".body")
    pr_url = adaptation_pr_reference(current_body, repo)
    pr_head_match = re.search(r"^PR head: `([a-f0-9]{40})`$", current_body, re.MULTILINE)
    status = ("WAITING_RUNTIME" if manifest.get("stage") == "static-verified-pending-runtime"
              else "ADAPTATION_RUNNING")
    diagnostics_summary = json.dumps(diagnostics, ensure_ascii=False, indent=2)[-12000:]
    result_path = directory / 'attempt-result.md'
    result_path.write_text(f"Adaptation result: `{status}`\nRun: {run_url}\n\n```json\n{diagnostics_summary}\n```\n")
    gh('issue', 'comment', str(issue), '--repo', repo, '--body-file', str(result_path))
    write_issue(repo, issue, issue_body(report, status, run_url, pr_url, run_url,
                expected_artifact, pr_head_match[1] if pr_head_match else ""), directory)
    append_github_output(Path(os.environ["GITHUB_OUTPUT"]), {"checkpoint_saved": "true", "consumed_comment_id": str(context.get("consumedCommentId", 0))})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=["pending", "select-report", "claim", "publish", "fail", "checkpoint"])
    parser.add_argument("--report", type=Path)
    parser.add_argument("--targets", type=Path)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--checkpoint-directory", type=Path)
    parser.add_argument("--candidate-key")
    parser.add_argument("--output-report", type=Path)
    parser.add_argument("--app-id", default="wechat")
    parser.add_argument("--issue", type=int)
    args = parser.parse_args()
    policy = get_policy(args.app_id)
    repo = os.environ["GITHUB_REPOSITORY"]
    run_url = f"https://github.com/{repo}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
    if args.stage == "pending":
        pending(repo, policy.app_id)
    elif args.stage == "select-report":
        select_candidate_report(args.report, args.checkpoint_directory or args.directory,
                                args.candidate_key or "", args.output_report)
    elif args.stage == "claim":
        claim(repo, json.loads(args.report.read_text()), json.loads(args.targets.read_text()), args.directory, run_url, app_id=policy.app_id)
    elif args.stage == "publish":
        state = json.loads((args.directory / "state.json").read_text())
        policy = get_policy(state.get("appId"))
        publish(repo, args.directory, args.issue, run_url, policy.app_id)
    elif args.stage == "checkpoint":
        record_checkpoint(repo, args.report, args.directory, args.issue, run_url, policy.app_id)
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
        write_issue(repo, args.issue, issue_body(report, "ADAPTATION_RUNNING", run_url,
                    resume.get("existingPr", ""), checkpoint_url, checkpoint_artifact,
                    resume.get("existingPrHead", ""), resume.get("previousBlockers", [])), args.directory)
        append_github_output(Path(os.environ["GITHUB_OUTPUT"]), {"pipeline_status": "ADAPTATION_RUNNING"})


if __name__ == "__main__":
    main()
