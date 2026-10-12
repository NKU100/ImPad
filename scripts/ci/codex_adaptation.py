"""Trusted controller for one Codex adaptation continuation and static validation."""

import argparse
import hashlib
import json
import os
import re
import signal
import subprocess
from pathlib import Path

from scripts.ci.app_policy import get_policy, policy_for_report, validate_targets
from scripts.ci.discover_latest_wechat import append_github_output, validate_official_apk_url
from scripts.ci.select_compatibility_window import select_regression_targets
from scripts.ci.static_regression import check_apk, ensure_apk


MODEL = "gpt-6-luna"
REASONING = "xhigh"
ADAPTABLE_STATUSES = {"NEEDS_HOOK_REVIEW", "STATIC_VERIFIED_PENDING_RUNTIME"}


def candidate_key(report):
    policy = policy_for_report(report)
    identity = report["identity"]
    code, digest = identity["versionCode"], identity["apkSha256"]
    if type(code) is not int or code <= 0 or not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest, re.IGNORECASE):
        raise ValueError("Candidate version code or SHA-256 is invalid")
    if identity.get("abi") not in policy.supported_abis:
        raise ValueError("Candidate ABI is not registered for this app")
    return f"{policy.app_id}-{identity['packageName']}-{identity['abi']}-{code}-{digest.lower()}"


def eligible_candidate(report, targets, app_id=None):
    policy = policy_for_report(report, app_id)
    validate_targets(targets, policy.app_id)
    if report.get("status") not in ADAPTABLE_STATUSES:
        return False
    candidate_key(report)
    identity = report["identity"]
    validate_official_apk_url(report["sourceUrl"], policy.official_apk_prefix)
    trusted = [p for p in targets if p.get("verificationStatus") in {"runtime-verified-local", "runtime-verified-hosted"}
               and p.get("identity", {}).get("packageName") == identity.get("packageName")
               and p.get("identity", {}).get("abi") == identity.get("abi")]
    if not trusted:
        raise ValueError("No runtime-verified baseline is available")
    baseline = max(trusted, key=lambda p: p["identity"]["versionCode"])["identity"]
    if any(identity.get(k) != baseline[k] for k in ["packageName", "abi", "signerSha256"]):
        raise ValueError("Candidate identity does not match the trusted baseline")
    if any(p["identity"]["versionCode"] == identity["versionCode"]
           and p["identity"].get("packageName") == identity["packageName"]
           and p["identity"].get("abi") == identity["abi"]
           and p["identity"].get("apkSha256", "").lower() == identity["apkSha256"].lower()
           for p in targets):
        return False
    if identity["versionCode"] < baseline["versionCode"]:
        raise ValueError("Candidate must not be older than the supported baseline")
    return True


def validate_profiles(before, after, report, app_id=None):
    policy = policy_for_report(report, app_id)
    candidate_key(report)
    validate_targets(after, policy.app_id)

    def profile_key(profile):
        identity = profile["identity"]
        return (identity["packageName"], identity["abi"], identity["versionCode"], identity["apkSha256"].lower())

    original = {profile_key(p): p for p in before}
    updated = {profile_key(p): p for p in after}
    if len(updated) != len(after):
        raise ValueError("Adaptation contains a duplicate profile identity")
    if len(after) != len(before) + 1:
        raise ValueError("Adaptation must add exactly one profile and retain existing profiles")
    if len(updated) != len(after) or any(updated.get(code) != profile for code, profile in original.items()):
        raise ValueError("Adaptation modified or removed an existing profile")
    identity = report["identity"]
    candidate_key_value = (
        identity["packageName"], identity["abi"], identity["versionCode"], identity["apkSha256"].lower(),
    )
    candidate = updated.get(candidate_key_value)
    if candidate is None or profile_key(candidate) != candidate_key_value or candidate["identity"] != identity:
        raise ValueError("Candidate profile identity must match the verified APK")
    if candidate.get("verificationStatus") != "static-verified":
        raise ValueError("Candidate must remain static-verified until runtime verification")
    if candidate.get("sourceUrl") != report["sourceUrl"]:
        raise ValueError("Candidate source must match the verified report")
    hooks = candidate.get("hooks", [])
    if len(hooks) != len(policy.required_hook_ids) or {h.get("id") for h in hooks} != set(policy.required_hook_ids):
        raise ValueError("Candidate hooks do not match the registered app policy")
    for hook in hooks:
        locator = hook.get("locator")
        descriptor = hook.get("expectedDescriptor")
        string_anchor = hook.get("stringAnchor")
        descriptor_is_explicit = isinstance(descriptor, str) and bool(descriptor.strip())
        anchor_is_explicit = isinstance(string_anchor, str) and bool(string_anchor.strip())
        valid_locator = (
            locator in (None, "STRING_ANCHOR") and anchor_is_explicit
            or locator == "EXACT_DESCRIPTOR" and string_anchor is None
            and not hook.get("safeForForwardInference", False)
        )
        if not descriptor_is_explicit or not valid_locator:
            raise ValueError("Candidate Hook descriptor or locator is invalid")


def validate_paths(paths, app_id="wechat"):
    policy = get_policy(app_id)
    app_test_prefix = policy.app_source_prefix.replace("/src/main/kotlin/", "/src/test/kotlin/", 1)
    shared_test_prefixes = [app_test_prefix]
    for source in policy.shared_source_paths:
        if source.startswith("compat-core/src/main/kotlin/"):
            shared_test_prefixes.append(source.split("/src/main/kotlin/", 1)[0] + "/src/test/kotlin/")
    for path in paths:
        if (path != policy.targets_path and path not in policy.shared_source_paths
                and not path.startswith((policy.app_source_prefix, *shared_test_prefixes))):
            raise ValueError(f"Agent edit is outside the allowed adaptation paths: {path}")


def run_bounded(command, cwd, env, log, timeout=None):
    with log.open("w") as output:
        process = subprocess.Popen(command, cwd=cwd, env=env, stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            result = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            raise TimeoutError(f"Command exceeded its {timeout}-second execution limit") from None
        except BaseException:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            raise
    if result:
        raise RuntimeError(f"Command failed with exit code {result}; raw output is retained only on the runner")


def gradle_command(app_id):
    command = ["./gradlew", "--offline", "--no-daemon",
               ":app:testDebugUnitTest", ":compat-core:test", ":compat-checker:test", ":app:assembleDebug"]
    if app_id == 'wecom':
        command[3:3] = ['--no-parallel', '--max-workers=2']
    return command


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def validate_existing_tests(worktree, existing_tests):
    for path, digest in existing_tests.items():
        if not (worktree / path).is_file() or sha256(worktree / path) != digest:
            raise ValueError("Existing regression tests must remain unchanged")


def git(cwd, *arguments):
    return subprocess.check_output(["git", *arguments], cwd=cwd).decode().strip()


def prepare(repository, report_path, candidate_apk, directory):
    report = json.loads(report_path.read_text())
    policy = policy_for_report(report)
    before = json.loads((repository / policy.targets_path).read_text())
    if not eligible_candidate(report, before, policy.app_id):
        raise ValueError("Candidate is not eligible for a new adaptation attempt")
    if sha256(candidate_apk) != report["identity"]["apkSha256"]:
        raise ValueError("Candidate APK does not match the verified report")
    directory.mkdir(parents=True, exist_ok=True)
    worktree = directory / "checkout"
    base = git(repository, "rev-parse", "HEAD")
    subprocess.run(["git", "clone", "--quiet", "--no-local", "--no-checkout",
                    str(repository.resolve()), str(worktree)], check=True)
    subprocess.run(["git", "remote", "remove", "origin"], cwd=worktree, check=True)
    subprocess.run(["git", "checkout", "--quiet", "--detach", base], cwd=worktree, check=True)
    agent_base = git(worktree, "rev-parse", "HEAD")
    apk_dir = worktree / "work/apks"
    apk_dir.mkdir(parents=True)
    # Hard links avoid duplicating large, already verified APK files on the runner.
    candidate_digest = report["identity"]["apkSha256"].lower()
    candidate_filename = f"candidate-{candidate_digest}.apk"
    os.link(candidate_apk, apk_dir / candidate_filename)
    regression = select_regression_targets(before, report["identity"]["versionCode"], policy.app_id,
                                          report["identity"]["packageName"], report["identity"]["abi"])
    apks = [(report["identity"]["versionName"], f"work/apks/{candidate_filename}", candidate_digest)]
    for target in regression:
        identity = target["identity"]
        cached = ensure_apk(target, repository / ".cache" / f"{policy.app_id}-apks", policy.app_id)
        digest = identity["apkSha256"].lower()
        destination = apk_dir / f"{identity['versionCode']}-{digest}.apk"
        os.link(cached, destination)
        apks.append((identity["versionName"], str(destination.relative_to(worktree)), digest))
    analysis = worktree / "work/analysis"
    analysis.mkdir(parents=True)
    (analysis / "candidate-report.json").write_text(json.dumps(report, indent=2))
    existing_tests = {str(path.relative_to(worktree)): sha256(path)
                      for module in ["app", "compat-core"]
                      for path in (worktree / module / "src/test").rglob("*") if path.is_file()}
    state = {"appId": policy.app_id, "targetsPath": policy.targets_path,
             "worktree": str(worktree), "base": base, "agent_base": agent_base, "before": before, "report": report,
             "apks": apks, "existing_tests": existing_tests}
    (directory / "state.json").write_text(json.dumps(state, indent=2))


def run_model(directory, prompt_path):
    from scripts.ci.codex_goal import run_model_goal
    run_model_goal(directory, prompt_path)


def validate(directory):
    state = json.loads((directory / "state.json").read_text())
    policy = get_policy(state.get("appId"))
    worktree = Path(state["worktree"])
    if git(worktree, "rev-parse", "HEAD") != state.get("agent_base", state["base"]):
        raise ValueError("Agent must leave adaptation changes uncommitted")
    tracked = git(worktree, "diff", "--name-only", state.get("agent_base", state["base"])).splitlines()
    new_files = git(worktree, "ls-files", "--others", "--exclude-standard").splitlines()
    validate_paths(tracked + new_files, policy.app_id)
    if any((worktree / path).is_symlink() for path in tracked + new_files):
        raise ValueError("Adaptation files must not be symbolic links")
    validate_existing_tests(worktree, state["existing_tests"])
    after = json.loads((worktree / policy.targets_path).read_text())
    validate_profiles(state["before"], after, state["report"], policy.app_id)
    candidate = state["report"]
    for version, apk, expected in state["apks"]:
        check_apk(worktree, Path(policy.targets_path), worktree / apk, expected,
                  directory / f"check-{version}-{expected}.log", offline=True, app_id=policy.app_id)
    run_bounded(gradle_command(policy.app_id),
                worktree, dict(os.environ), directory / "build.log", timeout=600)
    for path in new_files:
        subprocess.run(["git", "add", "--", path], cwd=worktree, check=True)
    subprocess.run(["git", "diff", "--check", state.get("agent_base", state["base"])], cwd=worktree, check=True)
    with (directory / "adaptation.patch").open("w") as patch:
        subprocess.run(["git", "diff", "--binary", state.get("agent_base", state["base"])], cwd=worktree, stdout=patch, check=True)
    report = dict(candidate, status="STATIC_VERIFIED_PENDING_RUNTIME", blockers=[],
                  checkedVersions=list(dict.fromkeys(version for version, _, _ in state["apks"])),
                  suggestedProfile=next(p for p in after if p["identity"] == candidate["identity"]))
    # Findings describe the successful exact-profile checks, not the pre-adaptation failure.
    report["hooks"] = [{"hookId": h["id"], "status": "UNIQUE_MATCH", "stringAnchor": h["stringAnchor"],
                        "selectedDescriptor": h["expectedDescriptor"], "candidateDescriptors": [h["expectedDescriptor"]],
                        "message": "Verified against the adapted profile by the shared checker"}
                       for h in report["suggestedProfile"]["hooks"]]
    (directory / "candidate-report.json").write_text(json.dumps(report, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=["eligibility", "prepare", "model", "validate"])
    parser.add_argument("--app-id", default="wechat")
    parser.add_argument("--repository", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--apk", type=Path)
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--prompt", type=Path)
    args = parser.parse_args()
    if args.stage == "eligibility":
        report = json.loads(args.report.read_text())
        policy = get_policy(args.app_id)
        targets = json.loads((args.repository / policy.targets_path).read_text())
        eligible = eligible_candidate(report, targets, policy.app_id)
        append_github_output(Path(os.environ["GITHUB_OUTPUT"]), {"adaptation_eligible": str(eligible).lower()})
        print("Codex adaptation eligible:", eligible)
    elif args.stage == "prepare":
        prepare(args.repository.resolve(), args.report, args.apk.resolve(), args.directory.resolve())
    elif args.stage == "model":
        run_model(args.directory.resolve(), args.prompt)
    else:
        validate(args.directory.resolve())


if __name__ == "__main__":
    main()
