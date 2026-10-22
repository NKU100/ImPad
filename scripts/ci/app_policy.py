"""Trusted application policies shared by compatibility and runtime CI."""
from dataclasses import dataclass
from typing import Optional
import re
import json
import hashlib
import subprocess
import io
import shlex
import tarfile
import tempfile
from urllib.parse import urlsplit
import argparse
import os
from pathlib import Path


@dataclass(frozen=True)
class AppPolicy:
    app_id: str
    display_name: str
    package_name: str
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


POLICY_SHARED_PATHS = (
    "app/src/main/kotlin/io/github/nku100/impad/ImPadModule.kt",
    "app/src/main/kotlin/io/github/nku100/impad/runtime/AppAdapter.kt",
    "app/src/main/kotlin/io/github/nku100/impad/runtime/AppAdapterRegistry.kt",
    "app/src/main/kotlin/io/github/nku100/impad/runtime/HookInstaller.kt",
    "app/src/main/kotlin/io/github/nku100/impad/runtime/RuleRuntimeResolver.kt",
    "compat-rules/src/main/kotlin/io/github/nku100/impad/rules/RuleCatalog.kt",
)


POLICIES = {
    "wechat": AppPolicy(
        app_id="wechat",
        display_name="WeChat",
        package_name="com.tencent.mm",
        required_hook_ids=("tablet", "login"),
        official_apk_prefix="https://dldir1v6.qq.com/weixin/android/",
        artifact_prefix="impad",
        adaptation_prompt=".github/codex/adapt-wechat.md",
        app_source_prefix="app/src/main/kotlin/io/github/nku100/impad/apps/wechat/",
        shared_source_paths=POLICY_SHARED_PATHS,
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
        required_hook_ids=("device_type", "app_pad", "service_pad"),
        official_apk_prefix="https://dldir1.qq.com/wework/work_weixin/",
        artifact_prefix="impad",
        adaptation_prompt=".github/codex/adapt-wecom.md",
        app_source_prefix="app/src/main/kotlin/io/github/nku100/impad/apps/wecom/",
        shared_source_paths=POLICY_SHARED_PATHS,
        supported_abis=("arm64-v8a",),
        signer_sha256="66896ae0aae48b9e963a3e034fa2ca813ca2f661f53619a92263a8a5c2e3f388",
        runtime_baseline="NO_TABLET_ENTRY",
        runtime_qr_page="LoginQrCodeForAndroidPad",
        runtime_ui_strategy="scripts.ci.apps.wecom.runtime_policy",
        discovery_strategy="scripts.ci.apps.wecom.discovery_policy",
    ),
    "qq": AppPolicy(
        app_id="qq", display_name="QQ", package_name="com.tencent.mobileqq",
        required_hook_ids=("device_type",),
        official_apk_prefix="https://downv6.qq.com/qqweb/QQ_1/android_apk/",
        artifact_prefix="impad", adaptation_prompt=".github/codex/adapt-qq.md",
        app_source_prefix="app/src/main/kotlin/io/github/nku100/impad/apps/qq/",
        shared_source_paths=POLICY_SHARED_PATHS,
        supported_abis=("arm64-v8a",),
        signer_sha256="ea6e97ad6c34f7039a9c6daba732c97d0e098e83ede2b4d52c76eb0184ac7a38",
        runtime_baseline="PHONE_REQUEST_APP_ID", runtime_qr_page="REQUEST_APP_ID_CONTRAST",
        runtime_ui_strategy="scripts.ci.apps.qq.runtime_policy",
        discovery_strategy="scripts.ci.apps.qq.discovery_policy",
    ),
}

def get_policy(app_id: str) -> AppPolicy:
    selected = app_id
    try:
        return POLICIES[selected]
    except KeyError:
        raise ValueError(f"Unknown or unregistered appId: {selected}") from None


def _git_blob(repository: Path, revision: str, path: str) -> bytes | None:
    result = subprocess.run(['git', 'show', f'{revision}:{path}'], cwd=repository,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return result.stdout if result.returncode == 0 else None


def _git_paths(repository: Path, revision: str, directory: str) -> list[str]:
    result = subprocess.run(['git', 'ls-tree', '-r', '--name-only', revision, '--', directory],
                            cwd=repository, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, check=True)
    return [path for path in result.stdout.splitlines() if path]


def _catalog_at_revision(repository: Path, revision: str, app_id: str) -> dict:
    with tempfile.TemporaryDirectory(prefix=f'impad-{app_id}-catalog-') as temporary:
        root = Path(temporary)
        archive = subprocess.run(['git', 'archive', revision], cwd=repository,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True).stdout
        with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
            bundle.extractall(root, filter='data')
        command = ['./gradlew', '--offline', '--no-daemon', ':compat-checker:run',
                   '--args=' + shlex.join(['catalog', '--app-id', app_id, '--report', str(root / 'catalog.json')])]
        subprocess.run(command, cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                       check=True, timeout=300)
        catalog = json.loads((root / 'catalog.json').read_text())
    if (not isinstance(catalog, dict) or catalog.get('schemaVersion') != 1
            or catalog.get('appId') != app_id or not isinstance(catalog.get('definitions'), list)
            or not isinstance(catalog.get('bindings'), list)
            or not isinstance(catalog.get('strategyIds'), list)):
        raise ValueError(f'Rule catalog at {revision} is malformed for {app_id}')
    return catalog


def _validate_catalog_extension(base_catalog: dict, proposed_catalog: dict, app_id: str) -> None:
    if (base_catalog.get('appId') != app_id or proposed_catalog.get('appId') != app_id
            or not isinstance(base_catalog.get('definitions'), list)
            or not isinstance(proposed_catalog.get('definitions'), list)
            or not isinstance(base_catalog.get('bindings'), list)
            or not isinstance(proposed_catalog.get('bindings'), list)
            or not isinstance(base_catalog.get('strategyIds'), list)
            or not isinstance(proposed_catalog.get('strategyIds'), list)):
        raise ValueError(f'Rule catalog is malformed for {app_id}')

    def definitions(rows):
        result = {}
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get('ruleId'), str):
                raise ValueError(f'Rule catalog definition is malformed for {app_id}')
            key = row['ruleId']
            normalized = dict(row)
            if isinstance(normalized.get('requiredTargetIds'), list):
                normalized['requiredTargetIds'] = sorted(normalized['requiredTargetIds'])
            if key in result:
                raise ValueError(f'Rule catalog has duplicate rule IDs for {app_id}')
            result[key] = normalized
        return result

    old_definitions = definitions(base_catalog['definitions'])
    new_definitions = definitions(proposed_catalog['definitions'])
    if any(new_definitions.get(rule_id) != definition
           for rule_id, definition in old_definitions.items()):
        raise ValueError(f'Existing rule catalog definitions are immutable for {app_id}')
    if not set(base_catalog['strategyIds']).issubset(set(proposed_catalog['strategyIds'])):
        raise ValueError(f'Existing rule strategies are immutable for {app_id}')

    old_bindings = base_catalog['bindings']
    new_bindings = proposed_catalog['bindings']
    for old in old_bindings:
        if not isinstance(old, dict):
            raise ValueError(f'Rule catalog binding is malformed for {app_id}')
        matches = [row for row in new_bindings if isinstance(row, dict)
                   and row.get('ruleId') == old.get('ruleId') and row.get('min') == old.get('min')]
        if (len(matches) != 1 or (old.get('max') is not None
                                  and matches[0].get('max') != old.get('max'))
                or (old.get('max') is None and matches[0].get('max') is not None
                    and (type(matches[0].get('max')) is not int
                         or matches[0]['max'] < old.get('min', 0)))):
            raise ValueError(f'Existing rule catalog bindings are immutable except forward closure for {app_id}')


def validate_rule_changes(base: str, head: str, repository: Path | None = None, *,
                          base_catalogs: dict | None = None,
                          proposed_catalogs: dict | None = None) -> None:
    """Reject mutation of accepted app-specific rules while permitting additive rules."""
    repository = Path(repository or Path.cwd()).resolve()
    if not isinstance(base, str) or not base or not isinstance(head, str) or not head:
        raise ValueError('Rule change validation requires trusted base and proposed head revisions')
    for revision in (base, head):
        subprocess.run(['git', 'cat-file', '-e', f'{revision}^{{tree}}'], cwd=repository,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=True)

    from scripts.ci.acceptance import load_records_bytes

    for app_id in ('qq', 'wecom'):
        policy = get_policy(app_id)
        acceptance_path = f'compatibility/{app_id}/acceptance.json'
        baseline_snapshot = _git_blob(repository, base, acceptance_path)
        if baseline_snapshot is None:
            raise ValueError(f'Trusted baseline acceptance snapshot is missing for {app_id}')
        trusted_catalog = (base_catalogs or {}).get(app_id)
        proposed = (proposed_catalogs or {}).get(app_id)
        if trusted_catalog is None:
            trusted_catalog = _catalog_at_revision(repository, base, app_id)
        if proposed is None:
            proposed = _catalog_at_revision(repository, head, app_id)
        _validate_catalog_extension(trusted_catalog, proposed, app_id)
        records = load_records_bytes(baseline_snapshot, app_id, False, catalog=trusted_catalog,
                                     repository=repository, source_path=acceptance_path)
        if not records:
            raise ValueError(f'Trusted baseline has no accepted rule records for {app_id}')
        if _git_blob(repository, head, acceptance_path) != baseline_snapshot:
            raise ValueError(f'Adaptation cannot modify the trusted acceptance snapshot for {app_id}')

        protected_roots = (
            policy.app_source_prefix,
            f'compat-rules/src/main/kotlin/io/github/nku100/impad/rules/{app_id}/',
        )
        protected_paths = set()
        for root in protected_roots:
            protected_paths.update(_git_paths(repository, base, root))

        for record in records:
            fingerprint = record.get('module', {}).get('fingerprint', {})
            sources = fingerprint.get('moduleSources', [])
            if not isinstance(sources, list):
                raise ValueError(f'Accepted module source identity is malformed for {app_id}')
            hashes = {row.get('path'): row.get('sha256') for row in sources if isinstance(row, dict)}
            for path in protected_paths:
                if path not in hashes:
                    continue
                baseline = _git_blob(repository, base, path)
                if baseline is None or hashlib.sha256(baseline).hexdigest() != hashes[path]:
                    raise ValueError(f'Trusted base source differs from the accepted module identity: {path}')

        for path in sorted(protected_paths):
            baseline = _git_blob(repository, base, path)
            proposed = _git_blob(repository, head, path)
            if baseline is None or proposed != baseline:
                raise ValueError(f'Existing rule or strategy source is immutable: {path}')


def policy_for_report(report: dict, requested_app_id: Optional[str] = None) -> AppPolicy:
    policy = get_policy(requested_app_id or report.get("appId"))
    if report.get("appId") != policy.app_id:
        raise ValueError("Candidate report appId does not match the requested app")
    identity = report.get("identity", {})
    if identity.get("packageName") != policy.package_name:
        raise ValueError("Candidate package does not match the registered app policy")
    return policy


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--app-id", required=True)
    parser.add_argument("--github-output", type=Path, required=True)
    args = parser.parse_args()
    policy = get_policy(args.app_id)
    values = {
        "app_id": policy.app_id,
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
            env_file.write(f"ARTIFACT_PREFIX={policy.artifact_prefix}\n")
            env_file.write(f"ADAPTATION_PROMPT={policy.adaptation_prompt}\n")


if __name__ == "__main__":
    main()
