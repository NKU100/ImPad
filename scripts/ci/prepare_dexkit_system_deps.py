"""Install or verify the locked Linux packages needed to build DexKit."""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import subprocess
from pathlib import Path

from scripts.ci.prepare_dexkit import DexKitPreparationError, platform_for_host


class SystemDependencyError(DexKitPreparationError):
    pass


PACKAGE_SPEC = re.compile(r"([a-z0-9][a-z0-9+.-]*)=([A-Za-z0-9.+:~_-]+)\Z")


def select_packages(lock: dict, system: str, architecture: str) -> tuple[str, list[str]]:
    selected = platform_for_host(system, architecture)
    if selected is None:
        raise SystemDependencyError(f"Unsupported Linux runner: {system} {architecture}")
    build = lock.get("build", {}).get(selected)
    packages = build.get("systemPackages") if isinstance(build, dict) else None
    if not isinstance(packages, list) or not packages:
        raise SystemDependencyError(f"Lock has no systemPackages for {selected}")
    if any(not isinstance(item, str) or not PACKAGE_SPEC.fullmatch(item) for item in packages):
        raise SystemDependencyError(f"Lock has an invalid exact package specification for {selected}")
    if len(set(packages)) != len(packages):
        raise SystemDependencyError(f"Lock repeats a system package for {selected}")
    return selected, packages


def installed_version(package: str) -> str | None:
    result = subprocess.run(
        ["dpkg-query", "-W", "-f=${Version}", package],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _apt(command: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=True,
            env={**os.environ, "DEBIAN_FRONTEND": "noninteractive"},
        )
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or "no apt diagnostic was returned").strip()
        raise SystemDependencyError(
            f"{ ' '.join(command) } failed with exit code {error.returncode}: {detail}"
        ) from error


def prepare_packages(packages: list[str], install: bool) -> str:
    expected = {match.group(1): match.group(2) for item in packages if (match := PACKAGE_SPEC.fullmatch(item))}
    if len(expected) != len(packages) or not expected:
        raise SystemDependencyError("Refusing an invalid locked apt package list")

    current = {name: installed_version(name) for name in expected}
    mismatches = {name: version for name, version in current.items()
                  if version is not None and version != expected[name]}
    if mismatches:
        details = ", ".join(f"{name} installed {version}, locked {expected[name]}"
                             for name, version in sorted(mismatches.items()))
        raise SystemDependencyError(
            f"Installed package version mismatch ({details}); refusing an implicit upgrade/downgrade. "
            "Use a clean Ubuntu 24.04 runner or resolve the host package state explicitly."
        )

    missing = [f"{name}={version}" for name, version in expected.items() if current[name] is None]
    if not missing:
        return "reused"
    if not install:
        raise SystemDependencyError(f"Locked packages are missing: {', '.join(missing)}; rerun with --install")

    _apt(["apt-get", "update"])
    _apt(["apt-get", "install", "--yes", "--no-install-recommends", *packages])

    unverified = {name: installed_version(name) for name in expected}
    failures = {name: (unverified[name], expected[name]) for name in expected
                if unverified[name] != expected[name]}
    if failures:
        detail = ", ".join(f"{name} installed {actual!r}, expected {wanted}"
                            for name, (actual, wanted) in sorted(failures.items()))
        raise SystemDependencyError(f"apt returned success but locked package verification failed: {detail}")
    return "installed"


def prepare(lock_path: Path, install: bool) -> dict:
    try:
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SystemDependencyError(f"Could not read DexKit lock: {error}") from error
    selected, packages = select_packages(lock, platform.system(), platform.machine())
    result = prepare_packages(packages, install)
    return {"platform": selected, "result": result, "packages": packages}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--install", action="store_true", help="install missing locked packages with apt")
    args = parser.parse_args()
    result = prepare(args.lock, args.install)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemDependencyError as error:
        raise SystemExit(f"DexKit system dependency preparation failed: {error}")
