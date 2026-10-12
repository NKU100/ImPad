"""Trusted application policies shared by compatibility and runtime CI."""
from dataclasses import dataclass
from typing import Optional
import re
import hashlib
from urllib.parse import urlsplit
import argparse
import os
from pathlib import Path


@dataclass(frozen=True)
class AppPolicy:
    app_id: str
    display_name: str
    package_name: str
    targets_path: str
    required_hook_ids: tuple[str, ...]
    official_apk_prefix: str
    artifact_prefix: str
    adaptation_prompt: str
    app_source_prefix: str
    shared_source_paths: tuple[str, ...]
    supported_abis: tuple[str, ...]
    signer_sha256: str
    runtime_baseline: str
    runtime_qr_page: str
    runtime_ui_strategy: str
    discovery_strategy: str


POLICIES = {
    "wechat": AppPolicy(
        app_id="wechat",
        display_name="WeChat",
        package_name="com.tencent.mm",
        targets_path="compatibility/wechat/targets.json",
        required_hook_ids=("tablet", "login"),
        official_apk_prefix="https://dldir1v6.qq.com/weixin/android/",
        artifact_prefix="impad",
        adaptation_prompt=".github/codex/adapt-wechat.md",
        app_source_prefix="app/src/main/kotlin/io/github/nku100/impad/apps/wechat/",
        shared_source_paths=(
            "app/src/main/kotlin/io/github/nku100/impad/ImPadModule.kt",
            "app/src/main/kotlin/io/github/nku100/impad/runtime/AppAdapter.kt",
            "app/src/main/kotlin/io/github/nku100/impad/runtime/AppAdapterRegistry.kt",
            "app/src/main/kotlin/io/github/nku100/impad/runtime/HookInstaller.kt",
            "app/src/main/kotlin/io/github/nku100/impad/runtime/RuntimeCompatibilityResolver.kt",
            "compat-core/src/main/kotlin/io/github/nku100/impad/compat/CandidatePipeline.kt",
            "compat-core/src/main/kotlin/io/github/nku100/impad/compat/CompatibilityResolver.kt",
            "compat-checker/src/main/kotlin/io/github/nku100/impad/checker/Main.kt",
        ),
        supported_abis=("arm64-v8a",),
        signer_sha256="0fe4ff85c215918396dadc7cd8ce6963339af33d37751a56e54c7206b63a3c7c",
        runtime_baseline="NO_TABLET_ENTRY",
        runtime_qr_page="LoginAsExDeviceUI",
        runtime_ui_strategy="scripts.ci.apps.wechat.runtime_policy",
        discovery_strategy="scripts.ci.apps.wechat.discovery_policy",
    ),
    "wecom": AppPolicy(
        app_id="wecom",
        display_name="WeCom",
        package_name="com.tencent.wework",
        targets_path="compatibility/wecom/targets.json",
        required_hook_ids=("device_type", "app_pad", "service_pad"),
        official_apk_prefix="https://dldir1.qq.com/wework/work_weixin/",
        artifact_prefix="impad",
        adaptation_prompt=".github/codex/adapt-wecom.md",
        app_source_prefix="app/src/main/kotlin/io/github/nku100/impad/apps/wecom/",
        shared_source_paths=(
            "app/src/main/kotlin/io/github/nku100/impad/ImPadModule.kt",
            "app/src/main/kotlin/io/github/nku100/impad/runtime/AppAdapter.kt",
            "app/src/main/kotlin/io/github/nku100/impad/runtime/AppAdapterRegistry.kt",
            "app/src/main/kotlin/io/github/nku100/impad/runtime/HookInstaller.kt",
            "app/src/main/kotlin/io/github/nku100/impad/runtime/RuntimeCompatibilityResolver.kt",
            "compat-core/src/main/kotlin/io/github/nku100/impad/compat/CandidatePipeline.kt",
            "compat-core/src/main/kotlin/io/github/nku100/impad/compat/CompatibilityResolver.kt",
            "compat-checker/src/main/kotlin/io/github/nku100/impad/checker/Main.kt",
        ),
        supported_abis=("arm64-v8a",),
        signer_sha256="66896ae0aae48b9e963a3e034fa2ca813ca2f661f53619a92263a8a5c2e3f388",
        runtime_baseline="NO_TABLET_ENTRY",
        runtime_qr_page="LoginQrCodeForAndroidPad",
        runtime_ui_strategy="scripts.ci.apps.wecom.runtime_policy",
        discovery_strategy="scripts.ci.apps.wecom.discovery_policy",
    ),
}


def get_policy(app_id: Optional[str] = None) -> AppPolicy:
    selected = "wechat" if app_id is None else app_id
    try:
        return POLICIES[selected]
    except KeyError:
        raise ValueError(f"Unknown or unregistered appId: {selected}") from None


def policy_for_report(report: dict, requested_app_id: Optional[str] = None) -> AppPolicy:
    policy = get_policy(requested_app_id or report.get("appId"))
    if report.get("appId", "wechat") != policy.app_id:
        raise ValueError("Candidate report appId does not match the requested app")
    identity = report.get("identity", {})
    if identity.get("packageName") != policy.package_name:
        raise ValueError("Candidate package does not match the registered app policy")
    return policy


def validate_targets(targets: list[dict], app_id: Optional[str] = None) -> AppPolicy:
    policy = get_policy(app_id)
    seen = set()
    if not targets:
        raise ValueError("Compatibility policy has no targets")
    for target in targets:
        identity = target.get("identity", {})
        if identity.get("packageName") != policy.package_name:
            raise ValueError("Compatibility target package does not match app policy")
        if identity.get("abi") not in policy.supported_abis:
            raise ValueError("Compatibility target ABI does not match app policy")
        if identity.get("signerSha256", "").lower() != policy.signer_sha256:
            raise ValueError("Compatibility target signer does not match app policy")
        if type(identity.get("versionCode")) is not int or identity["versionCode"] <= 0:
            raise ValueError("Compatibility target versionCode must be a positive integer")
        if not re.fullmatch(r"[a-fA-F0-9]{64}", identity.get("apkSha256", "")):
            raise ValueError("Compatibility target APK SHA-256 is invalid")
        hook_ids = [hook.get("id") for hook in target.get("hooks", [])]
        if len(hook_ids) != len(policy.required_hook_ids) or set(hook_ids) != set(policy.required_hook_ids):
            raise ValueError("Compatibility target hooks do not match app policy")
        key = (identity.get("packageName"), identity.get("abi"), identity.get("versionCode"),
               identity["apkSha256"].lower())
        if key in seen:
            raise ValueError("Duplicate compatibility profile identity")
        seen.add(key)
        source_url = target.get("sourceUrl", "")
        prefix = urlsplit(policy.official_apk_prefix)
        parsed = urlsplit(source_url)
        if (parsed.scheme != "https" or parsed.netloc != prefix.netloc or parsed.query or parsed.fragment
                or parsed.username or parsed.password
                or not re.fullmatch(re.escape(prefix.path) + r"[A-Za-z0-9._-]+[.]apk", parsed.path)):
            raise ValueError("Compatibility target source does not match app policy")
    return policy


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--app-id", default="wechat")
    parser.add_argument("--github-output", type=Path, required=True)
    args = parser.parse_args()
    policy = get_policy(args.app_id)
    values = {
        "app_id": policy.app_id,
        "targets_path": policy.targets_path,
        "artifact_prefix": policy.artifact_prefix,
        "adaptation_prompt": policy.adaptation_prompt,
    }
    with args.github_output.open("a") as output:
        for key, value in values.items():
            output.write(f"{key}={value}\n")
    github_env = os.environ.get("GITHUB_ENV")
    if github_env:
        with Path(github_env).open("a") as env_file:
            env_file.write(f"APP_ID={policy.app_id}\n")
            env_file.write(f"TARGETS_PATH={policy.targets_path}\n")
            env_file.write(f"ARTIFACT_PREFIX={policy.artifact_prefix}\n")
            env_file.write(f"ADAPTATION_PROMPT={policy.adaptation_prompt}\n")


if __name__ == "__main__":
    main()
