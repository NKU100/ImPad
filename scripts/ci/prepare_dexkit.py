"""Prepare the locked DexKit runtime inputs without exposing unverified files."""

from __future__ import annotations

import hashlib
import argparse
import json
import os
import platform as host_platform
import shutil
import subprocess
import tempfile
import tarfile
from urllib.parse import urlparse
import urllib.request
import zipfile
from pathlib import Path


class DexKitPreparationError(RuntimeError):
    pass


PLATFORMS = {
    "linux-aarch64": ("jvmApi", "linuxAarch64"),
    "linux-x86_64": ("jvmApi", "linuxX8664"),
    "android-arm64-v8a": ("androidAar", "androidArm64V8a"),
}


def platform_for_host(os_name: str, architecture: str) -> str | None:
    if "linux" not in os_name.lower():
        return None
    arch = architecture.lower()
    if arch in {"x86_64", "amd64", "x64"}:
        return "linux-x86_64"
    if arch in {"aarch64", "arm64"}:
        return "linux-aarch64"
    return None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _download_or_resolve(spec: dict, lock_dir: Path, temporary: Path) -> Path:
    if "file" in spec:
        path = (lock_dir / spec["file"]).resolve()
    elif "buildOutput" in spec:
        path = temporary / "source" / spec["buildOutput"]
    elif "url" in spec:
        name = Path(urlparse(spec["url"]).path).name or "artifact"
        path = temporary / name
        request = urllib.request.Request(spec["url"], headers={"User-Agent": "ImPad-DexKit-lock/1"})
        try:
            with urllib.request.urlopen(request, timeout=60) as response, path.open("wb") as target:
                shutil.copyfileobj(response, target)
        except Exception as error:
            raise DexKitPreparationError(f"Could not acquire {spec['url']}: {error}") from error
    else:
        raise DexKitPreparationError("Artifact must declare a file or URL")
    if not path.is_file():
        raise DexKitPreparationError(f"Artifact does not exist: {path}")
    actual_sha256 = _sha256(path)
    if actual_sha256 != spec.get("sha256"):
        raise DexKitPreparationError(
            f"SHA-256 mismatch for {path}: expected {spec.get('sha256')}, got {actual_sha256}"
        )
    return path


def _resolve_api(api_spec: dict, artifacts: dict, lock_dir: Path, temporary: Path) -> Path:
    source_artifact = api_spec.get("sourceArtifact")
    if source_artifact is None:
        return _download_or_resolve(api_spec, lock_dir, temporary)
    source_spec = artifacts.get(source_artifact)
    if not isinstance(source_spec, dict):
        raise DexKitPreparationError(f"JVM API references missing source artifact: {source_artifact}")
    source_path = _download_or_resolve(source_spec, lock_dir, temporary)
    member = api_spec.get("archiveMember")
    if not member:
        raise DexKitPreparationError("JVM API archive source must declare an archive member")
    try:
        with zipfile.ZipFile(source_path) as archive:
            content = archive.read(member)
    except (OSError, KeyError, zipfile.BadZipFile) as error:
        raise DexKitPreparationError(f"Could not read {member} from {source_artifact}: {error}") from error
    actual_sha256 = hashlib.sha256(content).hexdigest()
    if actual_sha256 != api_spec.get("sha256"):
        raise DexKitPreparationError(
            f"SHA-256 mismatch for {source_artifact}!{member}: expected {api_spec.get('sha256')}, got {actual_sha256}"
        )
    extracted = temporary / "dexkit.jar"
    extracted.write_bytes(content)
    return extracted


def _download_locked_tool(spec: dict, destination: Path) -> Path:
    request = urllib.request.Request(spec["url"], headers={"User-Agent": "ImPad-DexKit-lock/1"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response, destination.open("wb") as target:
            shutil.copyfileobj(response, target)
    except Exception as error:
        raise DexKitPreparationError(f"Could not acquire {spec['url']}: {error}") from error
    actual_sha256 = _sha256(destination)
    if actual_sha256 != spec.get("sha256"):
        raise DexKitPreparationError(
            f"SHA-256 mismatch for tool archive {spec['url']}: expected {spec.get('sha256')}, got {actual_sha256}"
        )
    return destination


def prepare_toolchain(build: dict, cache_root: Path | None = None) -> list[Path]:
    """Acquire locked Linux build tools into a user cache without changing system PATH."""
    cache_root = Path(cache_root or Path.home() / ".cache/impad/dexkit-toolchain")
    toolchain = build.get("toolchain")
    if not isinstance(toolchain, dict) or not {"cmake", "ninja"}.issubset(toolchain):
        raise DexKitPreparationError("Linux build lock must pin CMake and Ninja download hashes")
    installed_bins = []
    for name in ("cmake", "ninja"):
        spec = toolchain[name]
        version = build[f"{name}Version"]
        if spec.get("version", version) != version:
            raise DexKitPreparationError(f"Locked {name} download version does not match the required version")
        install = cache_root / f"{name}-{version}-{spec['sha256'][:12]}"
        executable = install / ("bin/cmake" if name == "cmake" else "ninja")
        marker = install / ".dexkit-archive-sha256"
        if not (executable.is_file() and marker.is_file() and marker.read_text().strip() == spec["sha256"]):
            install.parent.mkdir(parents=True, exist_ok=True)
            staging = Path(tempfile.mkdtemp(prefix=f".{install.name}-", dir=install.parent))
            try:
                archive = _download_locked_tool(spec, staging / "archive")
                if name == "cmake":
                    with tarfile.open(archive, "r:gz") as source:
                        members = source.getmembers()
                        root_name = spec["root"]
                        selected = [member for member in members if member.name == root_name or member.name.startswith(root_name + "/")]
                        if not selected:
                            raise DexKitPreparationError(f"CMake archive is missing expected directory {root_name}")
                        try:
                            source.extractall(staging, members=selected, filter="data")
                        except TypeError as error:
                            raise DexKitPreparationError("Python 3.12 or newer is required to safely extract CMake") from error
                    extracted = staging / root_name
                    if not (extracted / "bin/cmake").is_file():
                        raise DexKitPreparationError("CMake archive does not contain bin/cmake")
                    archive.unlink()
                    (extracted / ".dexkit-archive-sha256").write_text(spec["sha256"] + "\n", encoding="ascii")
                    if install.exists():
                        shutil.rmtree(install)
                    os.replace(extracted, install)
                    shutil.rmtree(staging, ignore_errors=True)
                else:
                    install.mkdir(parents=True, exist_ok=True)
                    with zipfile.ZipFile(archive) as source:
                        content = source.read(spec["member"])
                    executable.write_bytes(content)
                    executable.chmod(0o755)
                    marker.write_text(spec["sha256"] + "\n", encoding="ascii")
                    archive.unlink()
                    if staging.exists():
                        shutil.rmtree(staging)
            except Exception:
                shutil.rmtree(staging, ignore_errors=True)
                raise
        installed_bins.append(install / "bin" if name == "cmake" else install)
    return installed_bins


def _validate_lock(lock: dict, platform: str) -> tuple[dict, dict]:
    if platform not in PLATFORMS:
        raise DexKitPreparationError(f"Unsupported platform: {platform}")
    if lock.get("schemaVersion") != 1:
        raise DexKitPreparationError("Unsupported DexKit lock schema")
    commit = lock.get("source", {}).get("commit")
    if not isinstance(commit, str) or len(commit) != 40:
        raise DexKitPreparationError("Lock must pin a full upstream source commit")

    api_key, native_key = PLATFORMS[platform]
    artifacts = lock.get("artifacts", {})
    selected = {key: artifacts.get(key) for key in (api_key, native_key)}
    for key, spec in selected.items():
        if not isinstance(spec, dict):
            raise DexKitPreparationError(f"Missing locked artifact: {key}")
        if spec.get("sourceCommit") != commit:
            raise DexKitPreparationError(f"{key} was not produced from the locked source commit")
        sha256 = spec.get("sha256")
        if not isinstance(sha256, str) or len(sha256) != 64:
            raise DexKitPreparationError(f"{key} must have a locked SHA-256")
    return selected[api_key], selected[native_key]


def prepare(lock_path: Path, output: Path, platform: str) -> dict:
    lock_path = Path(lock_path)
    output = Path(output)
    try:
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise DexKitPreparationError(f"Could not read lock file: {error}") from error

    api_spec, native_spec = _validate_lock(lock, platform)
    if output.exists():
        return _verify_prepared_output(output, lock, platform, api_spec, native_spec)

    output.parent.mkdir(parents=True, exist_ok=True)
    staging_root = Path(tempfile.mkdtemp(prefix=f".{output.name}-", dir=output.parent))
    try:
        input_root = staging_root / "inputs"
        input_root.mkdir()
        build = lock.get("build", {}).get(platform)
        if build:
            checkout = _build_from_source(lock, platform, input_root / "source")
        api_path = _resolve_api(api_spec, lock.get("artifacts", {}), lock_path.parent, input_root)

        if platform == "android-arm64-v8a":
            member = native_spec.get("archiveMember")
            if not member:
                raise DexKitPreparationError("Android native lock must name its AAR member")
            try:
                with zipfile.ZipFile(api_path) as archive:
                    embedded = archive.read(member)
            except (OSError, KeyError, zipfile.BadZipFile) as error:
                raise DexKitPreparationError(f"Could not read {member} from the AAR: {error}") from error
            if hashlib.sha256(embedded).hexdigest() != native_spec["sha256"]:
                raise DexKitPreparationError("Embedded Android native SHA-256 mismatch")
            native_path = input_root / "libdexkit.so"
            native_path.write_bytes(embedded)
        else:
            if build:
                native_path = checkout / native_spec["buildOutput"]
                if not native_path.is_file() or _sha256(native_path) != native_spec["sha256"]:
                    raise DexKitPreparationError("Native source build did not produce the locked library")
            else:
                native_path = _download_or_resolve(native_spec, lock_path.parent, input_root)

        artifact_dir = staging_root / "artifacts"
        artifact_dir.mkdir()
        api_name = "dexkit.aar" if platform.startswith("android-") else "dexkit.jar"
        native_name = "libdexkit.so"
        shutil.copy2(api_path, artifact_dir / api_name)
        if platform.startswith("android-"):
            with zipfile.ZipFile(api_path) as archive:
                (artifact_dir / "dexkit.jar").write_bytes(archive.read("classes.jar"))
                (artifact_dir / native_name).write_bytes(embedded)
        else:
            shutil.copy2(native_path, artifact_dir / native_name)

        manifest = {
            "sourceCommit": lock["source"]["commit"],
            "platform": platform,
            "artifacts": {
                api_name: _sha256(artifact_dir / api_name),
                **({"dexkit.jar": _sha256(artifact_dir / "dexkit.jar")} if platform.startswith("android-") else {}),
                native_name: _sha256(artifact_dir / native_name),
            },
        }
        (artifact_dir / "dexkit-prepared.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        os.replace(artifact_dir, output)
        return _prepared_result(output, manifest)
    except Exception:
        shutil.rmtree(staging_root, ignore_errors=True)
        raise
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)


def _verify_prepared_output(output: Path, lock: dict, platform: str, api_spec: dict, native_spec: dict) -> dict:
    manifest_path = output / "dexkit-prepared.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise DexKitPreparationError(f"Could not read prepared manifest: {error}") from error

    if manifest.get("sourceCommit") != lock["source"]["commit"]:
        raise DexKitPreparationError("Prepared manifest source commit does not match the lock")
    if manifest.get("platform") != platform:
        raise DexKitPreparationError(f"Prepared manifest platform does not match {platform}")

    if platform == "android-arm64-v8a":
        aar_path = output / "dexkit.aar"
        if not aar_path.is_file():
            raise DexKitPreparationError(f"Prepared artifact is missing: {aar_path}")
        aar_hash = _sha256(aar_path)
        if aar_hash != api_spec["sha256"]:
            raise DexKitPreparationError(
                f"Prepared artifact dexkit.aar SHA-256 mismatch: expected {api_spec['sha256']}, got {aar_hash}"
            )
        try:
            with zipfile.ZipFile(aar_path) as archive:
                classes_jar = archive.read("classes.jar")
                embedded_native = archive.read(native_spec["archiveMember"])
        except (OSError, KeyError, zipfile.BadZipFile) as error:
            raise DexKitPreparationError(f"Invalid prepared Android AAR: {error}") from error
        if hashlib.sha256(embedded_native).hexdigest() != native_spec["sha256"]:
            raise DexKitPreparationError("Embedded Android native SHA-256 mismatch")
        if (output / "dexkit.jar").read_bytes() != classes_jar:
            raise DexKitPreparationError("Prepared Android API JAR does not match the locked AAR")
        if (output / "libdexkit.so").read_bytes() != embedded_native:
            raise DexKitPreparationError("Prepared Android native does not match the locked AAR")
        expected_artifacts = {
            "dexkit.aar": api_spec["sha256"],
            "dexkit.jar": hashlib.sha256(classes_jar).hexdigest(),
            "libdexkit.so": native_spec["sha256"],
        }
    else:
        expected_artifacts = {
            "dexkit.jar": api_spec["sha256"],
            "libdexkit.so": native_spec["sha256"],
        }

    expected_manifest = {
        "sourceCommit": lock["source"]["commit"],
        "platform": platform,
        "artifacts": expected_artifacts,
    }
    if manifest != expected_manifest:
        raise DexKitPreparationError("Prepared manifest artifacts/source do not match the lock")
    for name, expected_hash in expected_artifacts.items():
        artifact = output / name
        if not artifact.is_file():
            raise DexKitPreparationError(f"Prepared artifact is missing: {artifact}")
        actual_hash = _sha256(artifact)
        if actual_hash != expected_hash:
            raise DexKitPreparationError(
                f"Prepared artifact {name} SHA-256 mismatch: expected {expected_hash}, got {actual_hash}"
            )
    return _prepared_result(output, manifest)


def _prepared_result(output: Path, manifest: dict) -> dict:
    return {
        "sourceCommit": manifest["sourceCommit"],
        "platform": manifest["platform"],
        "directory": str(output.resolve()),
        "artifacts": {
            name: {"path": str((output / name).resolve()), "sha256": digest}
            for name, digest in manifest["artifacts"].items()
        },
    }


def _build_from_source(lock: dict, platform: str, source_dir: Path) -> Path:
    source = lock["source"]
    checkout = Path(source_dir)
    checkout.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "clone", "--filter=blob:none", "--no-checkout", source["repository"], str(checkout)],
        check=True,
    )
    subprocess.run(["git", "-C", str(checkout), "fetch", "--depth=1", "origin", source["commit"]], check=True)
    actual_commit = subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", "FETCH_HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if actual_commit != source["commit"]:
        raise DexKitPreparationError(f"Fetched source commit mismatch: {actual_commit}")
    subprocess.run(["git", "-C", str(checkout), "checkout", "--detach", actual_commit], check=True)
    expected_arch = {
        "linux-aarch64": {"aarch64", "arm64"},
        "linux-x86_64": {"x86_64", "amd64"},
    }.get(platform)
    if expected_arch is None or host_platform.machine().lower() not in expected_arch:
        raise DexKitPreparationError(f"Source build is not configured for {platform}")

    build = lock["build"][platform]
    tool_bins = prepare_toolchain(build)
    environment = os.environ.copy()
    environment["PATH"] = os.pathsep.join([*(str(path) for path in tool_bins), environment.get("PATH", "")])
    environment["CC"] = build.get("cCompiler", "gcc-13")
    environment["CXX"] = build.get("cxxCompiler", "g++-13")

    def require_version(command: list[str], expected: str) -> str:
        result = subprocess.run(command, check=True, capture_output=True, text=True, env=environment)
        version = result.stdout + result.stderr
        if expected not in version:
            raise DexKitPreparationError(f"Expected {expected} from {' '.join(command)}, got {version.strip()}")
        return version

    require_version(["cmake", "--version"], build["cmakeVersion"])
    require_version(["ninja", "--version"], build["ninjaVersion"])
    require_version([environment["CXX"], "--version"], build["cxxVersion"])
    native_build = checkout / "build-task1" / "native"
    native_source = checkout / "dexkit/src/main/cpp"
    subprocess.run(
        [
            "cmake", "-S", str(native_source), "-B", str(native_build), "-G", "Ninja",
            "-DCMAKE_BUILD_TYPE=Release",
            f"-DCMAKE_C_FLAGS_RELEASE=-O3 -DNDEBUG -ffile-prefix-map={checkout}=.",
            f"-DCMAKE_CXX_FLAGS_RELEASE=-O3 -DNDEBUG -ffile-prefix-map={checkout}=.",
            "-DDEXKIT_ENABLE_INTERNAL_METRICS=OFF",
            "-DDEXKIT_ENABLE_INTERNAL_METRICS_API=OFF",
        ],
        cwd=checkout,
        env=environment,
        check=True,
    )
    subprocess.run(
        ["cmake", "--build", str(native_build), "--parallel"],
        cwd=checkout,
        env=environment,
        check=True,
    )
    return checkout


def build_linux_from_source(lock_path: Path, source_dir: Path) -> dict:
    lock = json.loads(Path(lock_path).read_text(encoding="utf-8"))
    platform = platform_for_host(host_platform.system(), host_platform.machine())
    if platform is None:
        raise DexKitPreparationError("DexKit Linux source builds require a supported Linux host")
    checkout = _build_from_source(lock, platform, Path(source_dir))
    native_key = "linuxX8664" if platform == "linux-x86_64" else "linuxAarch64"
    native = checkout / lock["artifacts"][native_key]["buildOutput"]
    if not native.is_file():
        raise DexKitPreparationError("Linux source build did not produce the locked native library")
    return {
        "native": str(native),
        "nativeSha256": _sha256(native),
        "sourceCommit": lock["source"]["commit"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--platform", choices=["host", *sorted(PLATFORMS)], required=True)
    args = parser.parse_args()
    platform = args.platform
    if platform == "host":
        platform = platform_for_host(host_platform.system(), host_platform.machine()) or "android-arm64-v8a"
    result = prepare(args.lock, args.output, platform)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
