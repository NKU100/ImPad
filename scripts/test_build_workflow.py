"""Execute release-validation workflow selection and artifact contracts locally."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import textwrap
import unittest

from scripts.test_dexkit_gradle_integration import parsed_workflows


ROOT = Path(__file__).resolve().parents[1]


class BuildWorkflowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = parsed_workflows()['build.yml']
        cls.steps = cls.workflow['jobs']['build']['steps']

    def step(self, identifier):
        matches = [step for step in self.steps if step.get('id') == identifier]
        self.assertEqual(1, len(matches), identifier)
        return matches[0]

    def test_unit_test_step_runs_every_module_test_suite(self):
        step = self.step('gradle-tests')
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            gradle = root / 'gradlew'
            gradle.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > gradle-arguments\n')
            gradle.chmod(0o755)
            result = subprocess.run(['bash', '-euo', 'pipefail', '-c', step['run']],
                                    cwd=root, env=os.environ.copy(), text=True, capture_output=True)
            self.assertEqual(0, result.returncode, result.stderr)
            arguments = (root / 'gradle-arguments').read_text().splitlines()
            for task in (':compat-core:test', ':compat-rules:test', ':compat-checker:test',
                         ':app:testDebugUnitTest', ':app:testReleaseUnitTest', ':app:verifyModuleMetadata'):
                with self.subTest(task=task):
                    self.assertIn(task, arguments)

    def resolve(self, directory, **overrides):
        env = dict(os.environ, EVENT_NAME='workflow_dispatch', BUILD_REF='refs/heads/codex/r8',
                   BUILD_REPOSITORY='NKU100/ImPad', BUILD_ACTOR='NKU100',
                   BUILD_TRIGGERING_ACTOR='NKU100', RELEASE_VALIDATION='true', BUILD_TYPE='release',
                   GITHUB_OUTPUT=str(directory / 'outputs'))
        env.update(overrides)
        result = subprocess.run(['bash', '-euo', 'pipefail', '-c', self.step('validation-mode')['run']],
                                cwd=directory, env=env, text=True, capture_output=True)
        output = directory / 'outputs'
        return result, output.read_text() if output.exists() else ''

    @staticmethod
    def selected(step, context):
        expression = step.get('if', 'true').strip()
        expression = expression.removeprefix('${{').removesuffix('}}').strip()
        for key, value in sorted(context.items(), key=lambda item: -len(item[0])):
            expression = expression.replace(key, repr(value))
        expression = expression.replace('&&', ' and ').replace('||', ' or ')
        expression = re.sub(r'!(?!=)', 'not ', expression)
        return bool(eval(expression, {'__builtins__': {}, 'true': True,
                                      'startsWith': lambda value, prefix: value.startswith(prefix)}))

    def test_explicit_opt_in_accepts_only_owner_manual_feature_release(self):
        cases = [({}, 0, True), ({'RELEASE_VALIDATION': 'false'}, 0, False)]
        for key, value in [('EVENT_NAME', 'push'), ('EVENT_NAME', 'pull_request'),
                           ('BUILD_REF', 'refs/heads/main'), ('BUILD_REF', 'refs/tags/v1.0.0'),
                           ('BUILD_REF', 'refs/pull/1/merge'), ('BUILD_TYPE', 'debug'),
                           ('BUILD_REPOSITORY', 'someone/ImPad'), ('BUILD_ACTOR', 'someone'),
                           ('BUILD_TRIGGERING_ACTOR', 'someone')]:
            cases.append(({key: value}, 1, False))
        for overrides, exit_code, enabled in cases:
            with self.subTest(overrides=overrides), tempfile.TemporaryDirectory() as temporary:
                result, output = self.resolve(Path(temporary), **overrides)
                self.assertEqual(exit_code, result.returncode, result.stderr)
                self.assertEqual(enabled, 'enabled=true\n' in output)

    def test_workflow_routes_do_not_upload_debug_for_release_validation(self):
        for event, ref, opt_in, stable, debug, release in [
            ('push', 'refs/heads/codex/r8', False, False, True, False),
            ('pull_request', 'refs/pull/1/merge', False, False, True, False),
            ('workflow_dispatch', 'refs/heads/codex/r8', False, False, True, False),
            ('workflow_dispatch', 'refs/heads/codex/r8', True, False, False, True),
            ('push', 'refs/heads/main', False, True, False, False),
            ('push', 'refs/tags/v1.0.0', False, True, False, False),
            ('workflow_dispatch', 'refs/heads/main', False, True, False, False),
        ]:
            context = {'github.event_name': event, 'github.ref': ref,
                       'steps.validation-mode.outputs.enabled': str(opt_in).lower()}
            with self.subTest(event=event, ref=ref, opt_in=opt_in):
                self.assertEqual(stable, self.selected(self.step('stable-build'), context))
                self.assertEqual(debug, self.selected(self.step('debug-build'), context))
                self.assertEqual(release, self.selected(self.step('release-validation'), context))
                self.assertEqual(not opt_in, self.selected(self.step('upload-module'), context))
                self.assertEqual(opt_in, self.selected(self.step('upload-release-validation'), context))

    def test_validation_has_no_repository_secrets_and_reuses_static_gates(self):
        trigger = self.workflow.get('on', self.workflow.get('true'))
        self.assertEqual(False, trigger['workflow_dispatch']['inputs']['release_validation']['default'])
        self.assertEqual({'contents': 'read'}, self.workflow['permissions'])
        self.assertEqual({
            'EVENT_NAME': '${{ github.event_name }}', 'BUILD_REF': '${{ github.ref }}',
            'BUILD_REPOSITORY': '${{ github.repository }}', 'BUILD_ACTOR': '${{ github.actor }}',
            'BUILD_TRIGGERING_ACTOR': '${{ github.triggering_actor }}',
            'RELEASE_VALIDATION': '${{ inputs.release_validation || false }}',
            'BUILD_TYPE': "${{ inputs.build_type || 'auto' }}",
        }, self.step('validation-mode')['env'])
        validator = self.step('release-validation')
        self.assertFalse(any('secrets.' in str(value) for value in validator.get('env', {}).values()))
        validator_index = self.steps.index(validator)
        regression = next(i for i, step in enumerate(self.steps)
                          if 'python3 -m scripts.ci.static_regression --all-apps' in step.get('run', ''))
        self.assertLess(regression, validator_index)
        checkout = next(step for step in self.steps if step.get('name') == 'Check out source')
        self.assertEqual('${{ github.sha }}', checkout['with']['ref'])
        self.assertEqual('impad-r8-release-validation',
                         self.step('upload-release-validation')['with']['name'])

    def test_badge_publisher_isolated_from_build_and_only_trusts_main_build_artifacts(self):
        self.assertEqual({'contents': 'read'}, self.workflow['permissions'])
        triggers = self.workflow.get('on', self.workflow.get('true', self.workflow.get(True)))
        self.assertIn('!build-badge-data', triggers['push']['branches'])
        evidence = self.step('badge-evidence')
        self.assertEqual('always()', evidence['if'])
        badge_upload = next(step for step in self.steps
                            if step.get('name') == 'Upload build badge evidence')
        self.assertEqual('always()', badge_upload['if'])
        self.assertEqual('impad-build-badge', badge_upload['with']['name'])

        publisher = parsed_workflows()['publish-build-badge.yml']
        self.assertEqual({'actions': 'read', 'contents': 'write'}, publisher['permissions'])
        publisher_triggers = publisher.get('on', publisher.get('true', publisher.get(True)))
        self.assertIn('workflow_run', publisher_triggers)
        job = publisher['jobs']['publish']
        checkout = job['steps'][0]
        self.assertEqual('${{ github.event.repository.default_branch }}', checkout['with']['ref'])
        download = job['steps'][1]
        self.assertTrue(download['continue-on-error'])
        self.assertEqual('${{ github.event.workflow_run.id }}', download['with']['run-id'])
        self.assertNotIn('contents: write', self.workflow['permissions'])

    def test_python_unit_test_runner_and_build_badge_report_are_structured(self):
        self.assertEqual('python3 -m scripts.ci.run_unit_tests', self.step('python-tests')['run'])
        evidence = self.step('badge-evidence')
        self.assertIn('${{ steps.gradle-tests.outcome }}', evidence['env']['GRADLE_TESTS_OUTCOME'])
        self.assertIn('scripts.ci.build_badge write', evidence['run'])

    def build_fixture(self, root, failure=''):
        tools = root / 'bin'
        tools.mkdir()
        sdk = root / 'sdk'
        tool_commands = {
            'git': '#!/bin/bash\ncase "$1" in rev-parse) printf "%040d\\n" 1;; status) :;; archive) printf source-archive;; ls-files) :;; esac\n',
            'keytool': '#!/bin/bash\nwhile [[ $# -gt 0 ]]; do if [[ "$1" == -keystore ]]; then shift; printf key > "$1"; fi; shift; done\n',
            'openssl': '#!/bin/sh\nprintf temporary-test-password\n',
        }
        for name, body in tool_commands.items():
            path = tools / name
            path.write_text(body)
            path.chmod(0o755)
        for relative, body in {
            'build-tools/37.0.0/apksigner': '#!/bin/sh\n[ "$FAILURE" != signature ]\n',
            'build-tools/37.0.0/zipalign': '#!/bin/sh\n[ "$FAILURE" != alignment ]\n',
            'cmdline-tools/latest/bin/apkanalyzer': '#!/bin/sh\nif [ "$2" = debuggable ]; then echo false; else echo fixture-dex; fi\n',
        }.items():
            path = sdk / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body)
            path.chmod(0o755)
        lock = root / 'gradle/dexkit-lock.json'
        lock.parent.mkdir()
        lock.write_text(json.dumps({'artifacts': {'androidArm64V8a': {
            'sha256': hashlib.sha256(b'locked native').hexdigest()}}}))
        keep = root / 'app/src/release/keepRules/impad-module-entry.keep'
        keep.parent.mkdir(parents=True)
        keep.write_text('-keep class io.github.nku100.impad.ImPadModule\n')
        gradle = root / 'gradlew'
        gradle.write_text(textwrap.dedent('''\
            #!/usr/bin/env python3
            import hashlib, json, os, pathlib, sys, zipfile
            from scripts.ci.candidate_selection import module_source_hashes, source_tree_sha256
            root = pathlib.Path.cwd()
            assert sys.argv[1:] == ['--no-daemon', '--init-script', 'scripts/ci/gradle_failure_evidence.init.gradle', ':app:assembleRelease', ':app:verifyReleaseModuleMetadata']
            assert os.environ.get('WECHATPAD_KEYSTORE_BASE64') is None
            key = pathlib.Path(os.environ['WECHATPAD_KEYSTORE'])
            assert key.is_file()
            (root / 'key-path').write_text(str(key))
            output = root / 'app/build/outputs/apk/release/app-release.apk'
            output.parent.mkdir(parents=True)
            rows = module_source_hashes(root)
            fingerprint = {'schemaVersion': 1, 'moduleSources': rows, 'sourceTreeSha256': source_tree_sha256(root)}
            if os.environ['FAILURE'] == 'source': fingerprint['moduleSources'][0]['sha256'] = '0' * 64
            with zipfile.ZipFile(output, 'w') as archive:
                archive.writestr('assets/runtime-identity.json', json.dumps(fingerprint))
                archive.writestr('META-INF/xposed/java_init.list', 'io.github.nku100.impad.ImPadModule\\n')
                archive.writestr('lib/arm64-v8a/libdexkit.so', b'tampered' if os.environ['FAILURE'] == 'native' else b'locked native')
                archive.writestr('classes.dex', b'fixture dex')
            mapping = root / 'app/build/outputs/mapping/release'
            mapping.mkdir(parents=True)
            for name in ['mapping.txt', 'usage.txt', 'seeds.txt', 'configuration.txt']:
                (mapping / name).write_text('io.github.nku100.impad.ImPadModule\\n')
            if os.environ['FAILURE'] == 'r8': (mapping / 'seeds.txt').unlink()
        '''))
        gradle.chmod(0o755)
        env = dict(os.environ, PATH=str(tools) + os.pathsep + os.environ['PATH'],
                   ANDROID_HOME=str(sdk), RUNNER_TEMP=str(root / 'runner-temp'),
                   GITHUB_SHA='0' * 39 + '1', GITHUB_RUN_ID='123', GITHUB_REF='refs/heads/codex/r8',
                   GITHUB_REPOSITORY='NKU100/ImPad', PYTHONPATH=str(ROOT), FAILURE=failure,
                   WECHATPAD_KEYSTORE_BASE64='must-not-reach-gradle')
        (root / 'runner-temp').mkdir()
        result = subprocess.run(['bash', '-euo', 'pipefail', '-c', self.step('release-validation')['run']],
                                cwd=root, env=env, capture_output=True, text=True)
        return result

    def test_release_artifact_is_labelled_hashed_and_contains_no_key(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = self.build_fixture(root)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            artifact = root / 'work/r8-release-validation'
            metadata = json.loads((artifact / 'build.json').read_text())
            self.assertEqual('release', metadata['buildType'])
            self.assertEqual('R8', metadata['optimization'])
            self.assertEqual('ephemeral-test-key', metadata['signing'])
            self.assertIs(metadata['validationOnly'], True)
            apks = list(artifact.glob('*.apk'))
            self.assertEqual(1, len(apks))
            self.assertEqual(hashlib.sha256(apks[0].read_bytes()).hexdigest(), metadata['sha256'][apks[0].name])
            self.assertFalse(any(p.suffix in {'.jks', '.keystore', '.p12'} for p in artifact.rglob('*')))
            self.assertFalse(Path((root / 'key-path').read_text()).exists())
            from scripts.ci.stage_runtime_module import stage_module
            before = apks[0].read_bytes()
            self.assertEqual(hashlib.sha256(before).hexdigest(), stage_module(artifact))
            self.assertEqual(before, (artifact / 'app-debug.apk').read_bytes())

    def test_invalid_native_source_r8_signature_or_alignment_cannot_form_an_artifact(self):
        for failure in ['native', 'source', 'r8', 'signature', 'alignment']:
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                result = self.build_fixture(root, failure)
                self.assertNotEqual(0, result.returncode, result.stdout)
                self.assertFalse((root / 'work/r8-release-validation/build.json').exists())
                self.assertFalse(Path((root / 'key-path').read_text()).exists())


if __name__ == '__main__':
    unittest.main()
