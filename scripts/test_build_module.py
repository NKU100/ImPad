import base64
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).parent / 'ci' / 'build_module.sh'
SIGNING_NAMES = ['WECHATPAD_KEYSTORE_BASE64', 'WECHATPAD_KEYSTORE_PASSWORD',
                 'WECHATPAD_KEY_ALIAS', 'WECHATPAD_KEY_PASSWORD', 'WECHATPAD_KEYSTORE',
                 'WECHATPAD_DEBUG_KEYSTORE']


class BuildModuleTest(unittest.TestCase):
    def test_module_build_never_generates_or_packages_historical_targets(self):
        build = (Path(__file__).parents[1] / 'app/build.gradle.kts').read_text()
        self.assertNotIn('GenerateCompatibilityTargets', build)
        self.assertNotIn('resolution-modes.json', build)
        self.assertIn('assets/compatibility/', build)
        self.assertNotIn('targets.json', build)

    def test_module_build_generates_only_the_runtime_identity_asset(self):
        build = (Path(__file__).parents[1] / 'app/build.gradle.kts').read_text()
        self.assertIn('GenerateRuntimeIdentityAsset', build)
        self.assertIn('runtime-identity.json', build)
        self.assertIn('addGeneratedSourceDirectory', build)
        self.assertNotIn('generated/compatibility-assets', build)

    def test_gradle_fingerprint_excludes_only_the_qq_owner_harness(self):
        build = (Path(__file__).parents[1] / 'compat-rules/build.gradle.kts').read_text()
        self.assertIn('exclude("scripts/ci/apps/qq/probe/collector_owner_test.c")', build)
        self.assertNotIn('exclude("**/*_test.c")', build)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / '.gitignore').write_text('work/\napp/build/\ncalls\nkey-path\noutputs\n')
        (self.root / 'source.txt').write_text('versioned fixture source')
        gradle = self.root / 'gradlew'
        gradle.write_text('''#!/bin/sh
printf '%s\\n' "$*" >> calls
printf '%s' "${WECHATPAD_KEYSTORE:-}" > key-path
if test -n "${WECHATPAD_KEYSTORE:-}"; then test -s "$WECHATPAD_KEYSTORE" || exit 3; fi
case "$*" in *assembleRelease*) variant=release;; *) variant=debug;; esac
mkdir -p "app/build/outputs/apk/$variant"
printf 'fixture apk' > "app/build/outputs/apk/$variant/app-$variant.apk"
''')
        gradle.chmod(0o755)
        sdk = self.root / 'sdk'
        analyzer = sdk / 'cmdline-tools/latest/bin/apkanalyzer'
        analyzer.parent.mkdir(parents=True)
        analyzer.write_text('#!/bin/sh\ncase "$2" in version-name) echo 0.1.0;; version-code) echo 1;; esac\n')
        analyzer.chmod(0o755)
        signer = sdk / 'build-tools/37.0.0/apksigner'
        signer.parent.mkdir(parents=True)
        signer.write_text('#!/bin/sh\necho "fixture signer"\n')
        signer.chmod(0o755)
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        subprocess.run(['git', '-C', str(self.root), 'add', '.'], check=True)
        subprocess.run(['git', '-C', str(self.root), '-c', 'user.name=Fixture',
                        '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'fixture'], check=True)
        self.environment = {k: v for k, v in os.environ.items()
                            if k not in SIGNING_NAMES and not k.startswith('GITHUB_')}
        self.environment.update(ANDROID_HOME=str(sdk), GITHUB_OUTPUT=str(self.root / 'outputs'))

    def run_build(self, event, ref, requested='auto', signing=False):
        env = dict(self.environment, GITHUB_EVENT_NAME=event, GITHUB_REF=ref)
        if signing:
            env.update(WECHATPAD_KEYSTORE_BASE64=base64.b64encode(b'fixture key').decode(),
                       WECHATPAD_KEYSTORE_PASSWORD='fixture', WECHATPAD_KEY_ALIAS='fixture',
                       WECHATPAD_KEY_PASSWORD='fixture')
        return subprocess.run(['bash', str(SCRIPT.resolve()), requested], cwd=self.root,
                              env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    def test_main_and_version_tag_build_signed_release_with_source_and_checksums(self):
        result = self.run_build('push', 'refs/tags/v0.1.0', signing=True)
        self.assertEqual(0, result.returncode, result.stdout)
        calls = (self.root / 'calls').read_text()
        self.assertIn(':app:assembleRelease', calls)
        self.assertIn(':app:verifyReleaseModuleMetadata', calls)
        key = Path((self.root / 'key-path').read_text())
        self.assertFalse(key.exists())
        artifact = self.root / 'work/module-artifact'
        self.assertTrue((artifact / 'impad-0.1.0-release.apk').is_file())
        self.assertTrue((artifact / 'impad-0.1.0-source.tar.gz').is_file())
        self.assertIn('impad-0.1.0-release.apk', (artifact / 'SHA256SUMS').read_text())
        self.assertEqual('release', json.loads((artifact / 'build.json').read_text())['buildType'])

    def test_main_without_signing_fails_before_gradle_instead_of_falling_back(self):
        result = self.run_build('push', 'refs/heads/main')
        self.assertNotEqual(0, result.returncode)
        self.assertFalse((self.root / 'calls').exists())

    def test_main_selects_release(self):
        result = self.run_build('push', 'refs/heads/main', signing=True)
        self.assertEqual(0, result.returncode, result.stdout)
        self.assertIn(':app:assembleRelease', (self.root / 'calls').read_text())

    def test_pull_request_builds_debug_without_stable_secrets(self):
        result = self.run_build('pull_request', 'refs/pull/20/merge')
        self.assertEqual(0, result.returncode, result.stdout)
        self.assertIn(':app:assembleDebug', (self.root / 'calls').read_text())
        self.assertEqual('', (self.root / 'key-path').read_text())

    def test_pull_request_cannot_request_release(self):
        result = self.run_build('pull_request', 'refs/pull/20/merge', 'release', signing=True)
        self.assertNotEqual(0, result.returncode)
        self.assertFalse((self.root / 'calls').exists())

    def test_manual_debug_selection_is_honored(self):
        result = self.run_build('workflow_dispatch', 'refs/heads/main', 'debug', signing=True)
        self.assertEqual(0, result.returncode, result.stdout)
        self.assertIn(':app:assembleDebug', (self.root / 'calls').read_text())

    def test_temporary_branch_manual_debug_does_not_accept_stable_secrets(self):
        result = self.run_build('workflow_dispatch', 'refs/heads/temporary', 'debug', signing=True)
        self.assertNotEqual(0, result.returncode)
        self.assertFalse((self.root / 'calls').exists())

    def test_temporary_branch_manual_debug_works_without_stable_secrets(self):
        result = self.run_build('workflow_dispatch', 'refs/heads/temporary', 'debug')
        self.assertEqual(0, result.returncode, result.stdout)
        self.assertEqual('', (self.root / 'key-path').read_text())

    def test_tag_version_must_match_apk_version(self):
        result = self.run_build('push', 'refs/tags/v0.2.0', signing=True)
        self.assertNotEqual(0, result.returncode)
        self.assertFalse((self.root / 'work/module-artifact').exists())
