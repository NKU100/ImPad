import hashlib
import json
import tempfile
import unittest
import zipfile
import io
import tarfile
from pathlib import Path
from unittest.mock import patch

from scripts.ci import prepare_dexkit
from scripts.ci import prepare_dexkit_system_deps


class PrepareDexKitTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.lock_path = self.root / "dexkit-lock.json"
        self.output = self.root / "prepared"
        self.api = b"fixed api"
        self.native = b"fixed native"
        self.api_path = self.root / "dexkit.jar"
        self.native_path = self.root / "libdexkit.so"
        self.api_path.write_bytes(self.api)
        self.native_path.write_bytes(self.native)
        self.lock = {
            "schemaVersion": 1,
            "source": {
                "repository": "https://github.com/LuckyPray/DexKit.git",
                "commit": "a" * 40,
            },
            "artifacts": {
                "jvmApi": {
                    "file": "dexkit.jar",
                    "sha256": hashlib.sha256(self.api).hexdigest(),
                    "sourceCommit": "a" * 40,
                },
                "androidAar": {
                    "file": "dexkit.aar",
                    "sha256": "b" * 64,
                    "sourceCommit": "a" * 40,
                },
                "linuxAarch64": {
                    "file": "libdexkit.so",
                    "sha256": hashlib.sha256(self.native).hexdigest(),
                    "sourceCommit": "a" * 40,
                },
                "linuxX8664": {
                    "file": "libdexkit.so",
                    "sha256": hashlib.sha256(self.native).hexdigest(),
                    "sourceCommit": "a" * 40,
                },
            },
        }
        self._write_lock()

    def test_system_dependency_specs_are_selected_from_the_runner_architecture(self):
        lock = {
            "build": {
                "linux-x86_64": {"systemPackages": ["gcc-13=13.3-x86", "g++-13=13.3-x86", "zlib1g-dev=1.3-x86"]},
                "linux-aarch64": {"systemPackages": ["gcc-13=13.3-arm", "g++-13=13.3-arm", "zlib1g-dev=1.3-arm"]},
            }
        }

        self.assertEqual(
            prepare_dexkit_system_deps.select_packages(lock, "Linux", "x86_64"),
            ("linux-x86_64", ["gcc-13=13.3-x86", "g++-13=13.3-x86", "zlib1g-dev=1.3-x86"]),
        )
        self.assertEqual(
            prepare_dexkit_system_deps.select_packages(lock, "Linux", "aarch64"),
            ("linux-aarch64", ["gcc-13=13.3-arm", "g++-13=13.3-arm", "zlib1g-dev=1.3-arm"]),
        )
        repository_lock = json.loads((Path(__file__).resolve().parents[1] / "gradle/dexkit-lock.json").read_text())
        zlib_versions = {
            build_name: {
                package.split("=", 1)[0]: package.split("=", 1)[1]
                for package in build["systemPackages"]
                if package.startswith(("zlib1g=", "zlib1g-dev="))
            }
            for build_name, build in repository_lock["build"].items()
        }
        self.assertEqual(
            zlib_versions,
            {
                "linux-aarch64": {
                    "zlib1g-dev": "1:1.3.dfsg-3.1ubuntu2.3",
                    "zlib1g": "1:1.3.dfsg-3.1ubuntu2.3",
                },
                "linux-x86_64": {
                    "zlib1g-dev": "1:1.3.dfsg-3.1ubuntu2.3",
                    "zlib1g": "1:1.3.dfsg-3.1ubuntu2.3",
                },
            },
        )
        self.assertEqual(
            prepare_dexkit_system_deps.select_packages(repository_lock, "Linux", "x86_64")[0],
            "linux-x86_64",
        )
        self.assertEqual(
            prepare_dexkit_system_deps.select_packages(repository_lock, "Linux", "aarch64")[0],
            "linux-aarch64",
        )

    def test_exact_system_packages_are_reused_without_running_apt(self):
        packages = ["gcc-13=13.3.0-1", "g++-13=13.3.0-1", "zlib1g-dev=1.3-1"]
        with patch.object(prepare_dexkit_system_deps, "installed_version", side_effect=["13.3.0-1", "13.3.0-1", "1.3-1"]), \
                patch.object(prepare_dexkit_system_deps.subprocess, "run") as run:
            result = prepare_dexkit_system_deps.prepare_packages(packages, install=True)

        self.assertEqual(result, "reused")
        run.assert_not_called()

    def test_missing_locked_packages_are_installed_and_verified(self):
        packages = ["gcc-13=13.3.0-1", "g++-13=13.3.0-1", "zlib1g-dev=1.3-1"]
        with patch.object(prepare_dexkit_system_deps, "installed_version", side_effect=[None, None, None, "13.3.0-1", "13.3.0-1", "1.3-1"]), \
                patch.object(prepare_dexkit_system_deps.subprocess, "run") as run:
            result = prepare_dexkit_system_deps.prepare_packages(packages, install=True)

        self.assertEqual(result, "installed")
        commands = [call.args[0] for call in run.call_args_list]
        self.assertIn(["apt-get", "update"], commands)
        self.assertIn(
            ["apt-get", "install", "--yes", "--no-install-recommends", *packages],
            commands,
        )
        install_call = next(call for call in run.call_args_list if call.args[0][1:2] == ["install"])
        self.assertEqual(install_call.kwargs["env"]["DEBIAN_FRONTEND"], "noninteractive")

    def test_missing_locked_package_fails_in_verify_only_mode(self):
        with patch.object(prepare_dexkit_system_deps, "installed_version", return_value=None):
            with self.assertRaisesRegex(prepare_dexkit_system_deps.SystemDependencyError, "missing.*--install"):
                prepare_dexkit_system_deps.prepare_packages(
                    ["gcc-13=13.3.0-1", "g++-13=13.3.0-1", "zlib1g-dev=1.3-1"], install=False
                )

    def test_installed_version_mismatch_reports_explicit_downgrade_or_upgrade(self):
        packages = ["gcc-13=13.3.0-1", "g++-13=13.3.0-1", "zlib1g-dev=1.3-1"]
        with patch.object(prepare_dexkit_system_deps, "installed_version",
                          side_effect=["13.4.0-1", "13.3.0-1", "1.3-1"]):
            with self.assertRaisesRegex(prepare_dexkit_system_deps.SystemDependencyError, "gcc-13.*upgrade/downgrade"):
                prepare_dexkit_system_deps.prepare_packages(packages, install=True)

    def test_apt_conflict_is_reported_without_claiming_installation(self):
        packages = ["gcc-13=13.3.0-1", "g++-13=13.3.0-1", "zlib1g-dev=1.3-1"]
        failed = prepare_dexkit_system_deps.subprocess.CalledProcessError(
            100, ["apt-get", "install"], stderr="unmet dependencies: held package"
        )
        with patch.object(prepare_dexkit_system_deps, "installed_version", return_value=None), \
                patch.object(prepare_dexkit_system_deps.subprocess, "run", side_effect=[None, failed]):
            with self.assertRaisesRegex(prepare_dexkit_system_deps.SystemDependencyError, "apt-get install.*held package"):
                prepare_dexkit_system_deps.prepare_packages(packages, install=True)

    def _write_lock(self):
        self.lock_path.write_text(json.dumps(self.lock), encoding="utf-8")

    def test_rejects_hash_mismatch_without_publishing_artifacts(self):
        self.lock["artifacts"]["jvmApi"]["sha256"] = "0" * 64
        self._write_lock()

        with self.assertRaises(prepare_dexkit.DexKitPreparationError):
            prepare_dexkit.prepare(self.lock_path, self.output, "linux-aarch64")

        self.assertFalse(self.output.exists())

    def test_rejects_api_native_commit_mismatch(self):
        self.lock["artifacts"]["linuxAarch64"]["sourceCommit"] = "c" * 40
        self._write_lock()

        with self.assertRaises(prepare_dexkit.DexKitPreparationError):
            prepare_dexkit.prepare(self.lock_path, self.output, "linux-aarch64")

        self.assertFalse(self.output.exists())

    def test_refuses_unknown_platform(self):
        with self.assertRaises(prepare_dexkit.DexKitPreparationError):
            prepare_dexkit.prepare(self.lock_path, self.output, "freebsd-arm64")

        self.assertFalse(self.output.exists())

    def test_prepares_verified_linux_artifacts_and_records_hashes(self):
        result = prepare_dexkit.prepare(self.lock_path, self.output, "linux-aarch64")

        self.assertEqual(result["sourceCommit"], "a" * 40)
        self.assertEqual(result["artifacts"]["dexkit.jar"]["sha256"], hashlib.sha256(self.api).hexdigest())
        self.assertEqual(result["artifacts"]["libdexkit.so"]["sha256"], hashlib.sha256(self.native).hexdigest())
        self.assertEqual(Path(result["artifacts"]["dexkit.jar"]["path"]).read_bytes(), self.api)

    def test_reuses_missing_dependency_output_through_the_consumer_path(self):
        result = prepare_dexkit.prepare(self.lock_path, self.output, "linux-x86_64")

        self.assertEqual(Path(result["artifacts"]["dexkit.jar"]["path"]), (self.output / "dexkit.jar").resolve())
        self.assertTrue((self.output / "libdexkit.so").is_file())
        self.assertEqual(result["platform"], "linux-x86_64")

    def test_extracts_linux_api_only_from_the_checksum_locked_aar(self):
        aar = self.root / "dexkit.aar"
        classes = b"same published DexKit API"
        with zipfile.ZipFile(aar, "w") as archive:
            archive.writestr("classes.jar", classes)
        self.lock["artifacts"]["androidAar"] = {
            "file": "dexkit.aar",
            "sha256": hashlib.sha256(aar.read_bytes()).hexdigest(),
            "sourceCommit": "a" * 40,
        }
        self.lock["artifacts"]["jvmApi"] = {
            "sourceArtifact": "androidAar",
            "archiveMember": "classes.jar",
            "sha256": hashlib.sha256(classes).hexdigest(),
            "sourceCommit": "a" * 40,
        }
        self._write_lock()

        result = prepare_dexkit.prepare(self.lock_path, self.output, "linux-x86_64")

        self.assertEqual((self.output / "dexkit.jar").read_bytes(), classes)
        self.assertEqual(result["artifacts"]["dexkit.jar"]["sha256"], hashlib.sha256(classes).hexdigest())

    def test_reuses_an_existing_output_after_revalidating_every_artifact(self):
        first = prepare_dexkit.prepare(self.lock_path, self.output, "linux-aarch64")
        second = prepare_dexkit.prepare(self.lock_path, self.output, "linux-aarch64")

        self.assertEqual(first, second)

    def test_reuses_only_when_existing_api_and_native_match_the_lock(self):
        prepare_dexkit.prepare(self.lock_path, self.output, "linux-aarch64")
        (self.output / "dexkit.jar").write_bytes(b"tampered api")

        with self.assertRaisesRegex(prepare_dexkit.DexKitPreparationError, "dexkit.jar.*SHA-256"):
            prepare_dexkit.prepare(self.lock_path, self.output, "linux-aarch64")

    def test_rejects_existing_manifest_with_source_commit_mismatch(self):
        prepare_dexkit.prepare(self.lock_path, self.output, "linux-aarch64")
        manifest_path = self.output / "dexkit-prepared.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["sourceCommit"] = "c" * 40
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        with self.assertRaisesRegex(prepare_dexkit.DexKitPreparationError, "source commit"):
            prepare_dexkit.prepare(self.lock_path, self.output, "linux-aarch64")

    def test_rejects_existing_manifest_api_hash_that_differs_from_the_lock(self):
        prepare_dexkit.prepare(self.lock_path, self.output, "linux-aarch64")
        manifest_path = self.output / "dexkit-prepared.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["artifacts"]["dexkit.jar"] = "0" * 64
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        with self.assertRaisesRegex(prepare_dexkit.DexKitPreparationError, "manifest artifacts/source"):
            prepare_dexkit.prepare(self.lock_path, self.output, "linux-aarch64")

    def test_rejects_existing_native_that_does_not_match_the_lock(self):
        prepare_dexkit.prepare(self.lock_path, self.output, "linux-aarch64")
        (self.output / "libdexkit.so").write_bytes(b"tampered native")

        with self.assertRaisesRegex(prepare_dexkit.DexKitPreparationError, "libdexkit.so.*SHA-256"):
            prepare_dexkit.prepare(self.lock_path, self.output, "linux-aarch64")

    def test_selects_the_linux_platform_used_by_the_runner(self):
        self.assertEqual(prepare_dexkit.platform_for_host("Linux", "x86_64"), "linux-x86_64")
        self.assertEqual(prepare_dexkit.platform_for_host("Linux", "amd64"), "linux-x86_64")
        self.assertEqual(prepare_dexkit.platform_for_host("Linux", "aarch64"), "linux-aarch64")
        self.assertIsNone(prepare_dexkit.platform_for_host("Darwin", "arm64"))

    def test_bootstraps_locked_cmake_and_ninja_into_a_local_cache(self):
        cache = self.root / "tool-cache"
        cmake_archive = self.root / "cmake.tar.gz"
        ninja_archive = self.root / "ninja.zip"
        self._write_fake_cmake_archive(cmake_archive)
        self._write_fake_ninja_archive(ninja_archive)
        build = {
            "cmakeVersion": "3.28.3",
            "ninjaVersion": "1.11.1",
            "toolchain": {
                "cmake": {
                    "url": cmake_archive.as_uri(),
                    "sha256": hashlib.sha256(cmake_archive.read_bytes()).hexdigest(),
                    "format": "tar.gz",
                    "root": "cmake-3.28.3-linux-x86_64",
                },
                "ninja": {
                    "url": ninja_archive.as_uri(),
                    "sha256": hashlib.sha256(ninja_archive.read_bytes()).hexdigest(),
                    "format": "zip",
                    "member": "ninja",
                },
            },
        }

        tool_bin = prepare_dexkit.prepare_toolchain(build, cache)

        self.assertEqual((tool_bin[0] / "cmake").read_text(), "cmake-3.28.3")
        self.assertEqual((tool_bin[1] / "ninja").read_text(), "ninja-1.11.1")
        self.assertFalse((self.root / "cmake").exists())

    def _write_fake_cmake_archive(self, archive_path: Path):
        with tarfile.open(archive_path, "w:gz") as archive:
            info = tarfile.TarInfo("cmake-3.28.3-linux-x86_64/bin/cmake")
            content = b"cmake-3.28.3"
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))

    def _write_fake_ninja_archive(self, archive_path: Path):
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr("ninja", b"ninja-1.11.1")

    def test_rejects_actual_android_aar_checksum_mismatch(self):
        aar = self.root / "dexkit.aar"
        with zipfile.ZipFile(aar, "w") as archive:
            archive.writestr("classes.jar", b"android api")
            archive.writestr("jni/arm64-v8a/libdexkit.so", self.native)
        self.lock["artifacts"]["androidAar"] = {
            "file": "dexkit.aar",
            "sha256": "0" * 64,
            "sourceCommit": "a" * 40,
        }
        self.lock["artifacts"]["androidArm64V8a"] = {
            "archiveMember": "jni/arm64-v8a/libdexkit.so",
            "sha256": hashlib.sha256(self.native).hexdigest(),
            "sourceCommit": "a" * 40,
        }
        self._write_lock()

        with self.assertRaisesRegex(prepare_dexkit.DexKitPreparationError, "SHA-256 mismatch"):
            prepare_dexkit.prepare(self.lock_path, self.output, "android-arm64-v8a")

        self.assertFalse(self.output.exists())

    def test_android_consumer_output_contains_the_checksum_verified_aar_and_native(self):
        aar = self.root / "dexkit.aar"
        classes_jar = b"verified Android API"
        with zipfile.ZipFile(aar, "w") as archive:
            archive.writestr("classes.jar", classes_jar)
            archive.writestr("jni/arm64-v8a/libdexkit.so", self.native)
        self.lock["artifacts"]["androidAar"] = {
            "file": "dexkit.aar",
            "sha256": hashlib.sha256(aar.read_bytes()).hexdigest(),
            "sourceCommit": "a" * 40,
        }
        self.lock["artifacts"]["androidArm64V8a"] = {
            "archiveMember": "jni/arm64-v8a/libdexkit.so",
            "sha256": hashlib.sha256(self.native).hexdigest(),
            "sourceCommit": "a" * 40,
        }
        self._write_lock()

        prepared = prepare_dexkit.prepare(self.lock_path, self.output, "android-arm64-v8a")

        self.assertEqual(Path(prepared["artifacts"]["dexkit.aar"]["path"]).read_bytes(), aar.read_bytes())
        self.assertEqual((self.output / "dexkit.jar").read_bytes(), classes_jar)
        self.assertEqual((self.output / "libdexkit.so").read_bytes(), self.native)
        self.assertEqual(prepare_dexkit.prepare(self.lock_path, self.output, "android-arm64-v8a"), prepared)

    def test_rejects_embedded_android_native_mismatch_atomically(self):
        aar = self.root / "dexkit.aar"
        with zipfile.ZipFile(aar, "w") as archive:
            archive.writestr("classes.jar", b"android api")
            archive.writestr("jni/arm64-v8a/libdexkit.so", b"wrong native")
        self.lock["artifacts"]["androidAar"] = {
            "file": "dexkit.aar",
            "sha256": hashlib.sha256(aar.read_bytes()).hexdigest(),
            "sourceCommit": "a" * 40,
        }
        self.lock["artifacts"]["androidArm64V8a"] = {
            "archiveMember": "jni/arm64-v8a/libdexkit.so",
            "sha256": hashlib.sha256(b"expected native").hexdigest(),
            "sourceCommit": "a" * 40,
        }
        self._write_lock()

        with self.assertRaises(prepare_dexkit.DexKitPreparationError):
            prepare_dexkit.prepare(self.lock_path, self.output, "android-arm64-v8a")

        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
