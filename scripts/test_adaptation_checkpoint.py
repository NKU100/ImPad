import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from scripts.ci.codex_adaptation import sha256

from scripts.ci.adaptation_checkpoint import capture_checkpoint, restore_checkpoint


class AdaptationCheckpointTest(unittest.TestCase):
    def test_shared_and_per_app_workflows_wire_unbounded_checkpoint_resume(self):
        root = Path(__file__).resolve().parents[1]
        shared = (root / '.github/workflows/compatibility-app.yml').read_text()
        wechat = (root / '.github/workflows/compatibility-wechat.yml').read_text()
        wecom = (root / '.github/workflows/compatibility-wecom.yml').read_text()
        for required in [
            'id: claim', 'checkpoint_run_id', 'run-id:', 'github-token:',
            'adaptation_checkpoint restore', 'adaptation_checkpoint capture',
            'checkpoint.patch', 'handoff.json', 'codex_task checkpoint',
            'pending_adaptation', 'Find the oldest unfinished adaptation',
            'Select the report pinned by the unfinished adaptation',
        ]:
            with self.subTest(required=required):
                self.assertIn(required, shared, f'missing workflow element: {required}')
        for name, workflow, app_id in [('WeChat', wechat, 'wechat'), ('WeCom', wecom, 'wecom')]:
            with self.subTest(workflow=name):
                self.assertIn(f'app_id: {app_id}', workflow)
                self.assertNotIn('goal_token_budget', workflow)
                self.assertNotIn('retry_adaptation', workflow)
        self.assertNotIn('goal_token_budget', shared)
        self.assertNotIn('GOAL_TOKEN_BUDGET', shared)
        self.assertIn('continue-on-error: true', shared)
        self.assertIn('scripts.test_adaptation_checkpoint', shared)
        self.assertIn('runs-on: ubuntu-24.04-arm', shared)
        self.assertIn('scripts.ci.redroid_adaptation', shared)
        self.assertNotIn('android-emulator-runner', shared)
        self.assertIn('checkpoint-request.json', (root / 'scripts/ci/codex_live.mjs').read_text())
        self.assertIn('Match the latest APK to the unfinished adaptation', shared)
        self.assertIn('steps.candidate-checkpoint.outputs.same_candidate', shared)

    def test_checkpoint_artifact_credentials_are_exposed_to_production_controller(self):
        root = Path(__file__).resolve().parents[1]
        shared = (root / '.github/workflows/compatibility-app.yml').read_text()
        credential_step = shared.split('      - name: Expose masked artifact transport credentials to trusted controller', 1)[1].split('      - name:', 1)[0]
        self.assertIn('ACTIONS_RUNTIME_TOKEN', credential_step)
        self.assertIn('ACTIONS_RESULTS_URL', credential_step)
        self.assertIn('ACTIONS_RUNTIME_URL', credential_step)
        self.assertIn('core.setSecret', credential_step)
        self.assertIn('core.exportVariable', credential_step)

    def test_owner_feedback_resume_skips_discovery_based_window_selection(self):
        shared = (Path(__file__).resolve().parents[1]/'.github/workflows/compatibility-app.yml').read_text()
        step = shared.split('      - name: Select the latest-three regression window', 1)[1].split('      - name:', 1)[0]
        self.assertIn("steps.pending-adaptation.outputs.pending_adaptation != 'true'", step)
        self.assertIn("targets: ${{ steps.targets.outputs.matrix || '[]' }}", shared)

    def test_runtime_replay_is_not_suppressed_by_skipped_discovery_dependencies(self):
        shared = (Path(__file__).resolve().parents[1]/'.github/workflows/compatibility-app.yml').read_text()
        runtime = shared.split('\n  runtime:', 1)[1].split('\n  runtime-result:', 1)[0]
        self.assertIn("if: ${{ !cancelled() && needs.adapt.outputs.pr_url != '' }}", runtime)

    def test_common_workflow_locks_before_jobs_and_is_called_by_each_app(self):
        root = Path(__file__).resolve().parents[1]
        shared = (root / '.github/workflows/compatibility-app.yml').read_text()
        header, jobs = shared.split('\njobs:', 1)
        self.assertIn('\nconcurrency:\n  group: impad-codex-managed-auth\n  cancel-in-progress: false', header)
        self.assertNotIn('concurrency:', jobs)
        self.assertNotIn('  workflow_dispatch:', header)
        self.assertIn('  workflow_call:', header)
        self.assertNotIn('queue: max', shared)
        for app in ('wechat', 'wecom', 'qq'):
            shell = (root / f'.github/workflows/compatibility-{app}.yml').read_text()
            self.assertIn('uses: ./.github/workflows/compatibility-app.yml', shell)
            self.assertNotIn('concurrency:', shell)

    def test_runtime_validation_is_shared_by_manual_replay_and_adaptation(self):
        root = Path(__file__).resolve().parents[1]
        shared = (root / '.github/workflows/compatibility-app.yml').read_text()
        validation = (root / '.github/workflows/runtime-validation.yml').read_text()
        self.assertIn('uses: ./.github/workflows/runtime-validation.yml', shared)
        self.assertFalse((root / '.github/workflows/runtime-smoke.yml').exists())
        self.assertIn('  workflow_dispatch:', validation)
        self.assertIn('  workflow_call:', validation)
        build = validation.split('      - name: Check real APK and build replay module', 1)[1].split('      - name:', 1)[0]
        self.assertIn("if: inputs.module_run_id == ''", build)
        result = validation.split('  result:', 1)[1]
        self.assertIn("inputs.module_run_id == ''", result)
        self.assertIn('runtime-smoke/evidence/smoke-report.json', result)

    def test_abrupt_exit_reuses_code_and_session_from_same_published_snapshot(self):
        from scripts.ci.adaptation_checkpoint import reuse_published_checkpoint
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root); snapshot = directory/'live-checkpoints/1/1';snapshot.mkdir(parents=True)
            for name, contents in [('checkpoint.json','{"stage":"adaptation-in-progress"}'), ('checkpoint.patch','completed-turn-code'), ('handoff.json','{}'), ('native-session.tar.age','completed-turn-session')]:
                (snapshot/name).write_text(contents)
                (directory/name).write_text('interrupted-turn')
            (directory/'last-published-checkpoint.json').write_text(json.dumps({'directory':str(snapshot)}))
            reuse_published_checkpoint(directory)
            self.assertEqual('completed-turn-code', (directory/'checkpoint.patch').read_text())
            self.assertEqual('completed-turn-session', (directory/'native-session.tar.age').read_text())

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repository = self.root / 'repository'
        self.repository.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.repository)], check=True)
        self.source = self.repository / 'app/src/main/kotlin/io/github/nku100/impad/apps/wecom/Adapter.kt'
        self.source.parent.mkdir(parents=True)
        self.source_relative = self.source.relative_to(self.repository)
        self.source.write_text('class Adapter { val tablet = false }\n')
        self.existing_test_relative = 'app/src/test/kotlin/io/github/nku100/impad/apps/wecom/ExistingTest.kt'
        existing_test = self.repository / self.existing_test_relative
        existing_test.parent.mkdir(parents=True)
        existing_test.write_text('class ExistingTest\n')
        subprocess.run(['git', 'add', '.'], cwd=self.repository, check=True)
        subprocess.run(['git', '-c', 'user.name=Test', '-c', 'user.email=test@localhost',
                        'commit', '-qm', 'baseline'], cwd=self.repository, check=True)
        self.base = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=self.repository, text=True).strip()
        self.identity = {
            'packageName': 'com.tencent.wework', 'abi': 'arm64-v8a', 'versionName': '5.0.12',
            'versionCode': 83835, 'apkSha256': 'a' * 64,
            'signerSha256': '66896ae0aae48b9e963a3e034fa2ca813ca2f661f53619a92263a8a5c2e3f388',
        }
        self.task = self.root / 'task'
        self.checkout = self.task / 'checkout'
        self.checkout.parent.mkdir()
        subprocess.run(['git', 'clone', '-q', str(self.repository), str(self.checkout)], check=True)
        self.state = {
            'appId': 'wecom', 'targetsPath': 'compatibility/wecom/targets.json',
            'base': self.base, 'agent_base': self.base, 'worktree': str(self.checkout),
            'report': {'appId': 'wecom', 'identity': self.identity, 'status': 'NEEDS_HOOK_REVIEW'},
            'existing_tests': {self.existing_test_relative: sha256(existing_test)},
        }
        (self.task / 'state.json').write_text(json.dumps(self.state))
        self.checkpoint_dir = self.root / 'checkpoint'
        (self.task / 'resume-context.json').write_text(json.dumps({'status': 'ADAPTATION_RUNNING'}))

    def test_restore_preserves_published_tree_before_owner_changes(self):
        self.source_in_checkout = self.checkout / self.source_relative
        self.source_in_checkout.write_text('class Adapter { val tablet = true }\n')
        self.record_validation()
        capture_checkpoint(self.task)
        subprocess.run(['git', 'add', str(self.source_relative)], cwd=self.checkout, check=True)
        original_tree = subprocess.check_output(['git', 'write-tree'], cwd=self.checkout, text=True).strip()
        self.checkpoint_dir.mkdir()
        import shutil
        for name in ('checkpoint.json', 'checkpoint.patch', 'handoff.json'):
            shutil.copyfile(self.task / name, self.checkpoint_dir / name)
        subprocess.run(['git', 'reset', '--hard', self.base], cwd=self.checkout, check=True, stdout=subprocess.DEVNULL)
        restore_checkpoint(self.task, self.checkpoint_dir, {'checkpointRunId': '1234', 'ownerComments': [{'id': 1, 'body': 'change it'}]})
        context = json.loads((self.task / 'resume-context.json').read_text())
        self.assertEqual([{'base': self.base, 'tree': original_tree, 'runId': '1234'}], context.get('publicationCheckpoints'))
        self.source_in_checkout.write_text('class Adapter { val tablet = true; val correction = 1 }\n')
        self.assertEqual(original_tree, context['publicationCheckpoints'][0]['tree'])

    def test_repeated_restore_keeps_original_publication_tree_after_another_failed_publish(self):
        import shutil
        source = self.checkout / self.source_relative
        source.write_text('class Adapter { val tablet = true }\n')
        self.record_validation()
        capture_checkpoint(self.task)
        self.checkpoint_dir.mkdir()
        for name in ('checkpoint.json', 'checkpoint.patch', 'handoff.json'):
            shutil.copyfile(self.task / name, self.checkpoint_dir / name)
        subprocess.run(['git', 'reset', '--hard', self.base], cwd=self.checkout, check=True, stdout=subprocess.DEVNULL)
        restore_checkpoint(self.task, self.checkpoint_dir)
        original = json.loads((self.task / 'resume-context.json').read_text())['publicationCheckpoints']
        source.write_text('class Adapter { val tablet = true; val correction = 1 }\n')
        self.record_validation()
        capture_checkpoint(self.task)
        for name in ('checkpoint.json', 'checkpoint.patch', 'handoff.json'):
            shutil.copyfile(self.task / name, self.checkpoint_dir / name)
        subprocess.run(['git', 'reset', '--hard', self.base], cwd=self.checkout, check=True, stdout=subprocess.DEVNULL)
        restore_checkpoint(self.task, self.checkpoint_dir)
        recovered = json.loads((self.task / 'resume-context.json').read_text())['publicationCheckpoints']
        self.assertEqual(original[0], recovered[0])
        self.assertEqual(2, len(recovered), 'Both the old remote tree and newly restored tree can have been published')

    def tearDown(self):
        self.temp.cleanup()

    def record_validation(self):
        patch = subprocess.check_output(['git', 'diff', '--binary', '--full-index', self.base], cwd=self.checkout)
        (self.task / 'adaptation.patch').write_bytes(patch)
        report = dict(self.state['report'], status='STATIC_VERIFIED_PENDING_RUNTIME',
                      validatedPatchSha256=sha256(self.task / 'adaptation.patch'), validatedBaseCommit=self.base)
        (self.task / 'candidate-report.json').write_text(json.dumps(report))

    def fresh_task(self, name='restored'):
        task = self.root / name
        checkout = task / 'checkout'
        subprocess.run(['git', 'clone', '-q', str(self.repository), str(checkout)], check=True)
        (task / 'state.json').write_text(json.dumps(dict(self.state, worktree=str(checkout))))
        return task, checkout

    def test_first_capture_saves_the_input_report_before_static_validation(self):
        manifest = capture_checkpoint(self.task)
        self.assertTrue((self.task / 'candidate-report.json').is_file())
        self.assertEqual(self.state['report'], json.loads((self.task / 'candidate-report.json').read_text()))
        self.assertEqual('adaptation-in-progress', manifest['stage'])

    def test_new_app_and_core_tests_survive_checkpoint_restore(self):
        paths = ['app/src/test/kotlin/io/github/nku100/impad/apps/wecom/NewTest.kt',
                 'compat-core/src/test/kotlin/io/github/nku100/impad/compat/NewCoreTest.kt']
        for relative in paths:
            path = self.checkout / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('class RegressionTest\n')
        manifest = capture_checkpoint(self.task)
        task, checkout = self.fresh_task()
        restore_checkpoint(task, self.task)
        for relative in paths:
            with self.subTest(path=relative):
                self.assertNotIn(relative, manifest['excludedPaths'])
                self.assertTrue((checkout / relative).is_file())
                self.assertEqual('class RegressionTest\n', (checkout / relative).read_text())
        self.assertEqual('class ExistingTest\n', (checkout / self.existing_test_relative).read_text())

    def test_bound_validation_preserves_a_new_staged_test_on_restore(self):
        relative = 'app/src/test/kotlin/io/github/nku100/impad/apps/wecom/NewTest.kt'
        (self.checkout / relative).write_text('class NewTest\n')
        subprocess.run(['git', 'add', relative], cwd=self.checkout, check=True)
        self.record_validation()
        manifest = capture_checkpoint(self.task)
        self.assertEqual('static-verified-pending-runtime', manifest['stage'])
        task, checkout = self.fresh_task()
        self.assertFalse(restore_checkpoint(task, self.task, {'status': 'WAITING_RUNTIME'})['needsModel'])
        self.assertEqual('class NewTest\n', (checkout / relative).read_text())

    def test_capture_excludes_a_new_test_symlink(self):
        relative = 'app/src/test/kotlin/io/github/nku100/impad/apps/wecom/LinkedTest.kt'
        (self.checkout / relative).symlink_to(self.repository / self.existing_test_relative)
        manifest = capture_checkpoint(self.task)
        self.assertIn(relative, manifest['excludedPaths'])
        self.assertNotIn('LinkedTest.kt', (self.task / 'checkpoint.patch').read_text())
        task, checkout = self.fresh_task()
        restore_checkpoint(task, self.task)
        self.assertFalse((checkout / relative).is_symlink())

    def test_capture_rejects_modified_existing_tests(self):
        (self.checkout / self.existing_test_relative).write_text('class WeakenedTest\n')
        with self.assertRaisesRegex(ValueError, 'Existing regression tests'):
            capture_checkpoint(self.task)

    def test_restore_rejects_a_checkpoint_that_modifies_existing_tests(self):
        capture_checkpoint(self.task)
        (self.checkout / self.existing_test_relative).write_text('class WeakenedTest\n')
        patch_path = self.task / 'checkpoint.patch'
        patch_path.write_bytes(subprocess.check_output(['git', 'diff', '--binary', '--full-index', self.base], cwd=self.checkout))
        manifest_path = self.task / 'checkpoint.json'
        manifest = json.loads(manifest_path.read_text())
        manifest['patchSha256'] = sha256(patch_path)
        manifest_path.write_text(json.dumps(manifest))
        task, _ = self.fresh_task()
        with self.assertRaisesRegex(ValueError, 'Existing regression tests'):
            restore_checkpoint(task, self.task)

    def test_changed_patch_invalidates_static_stage(self):
        self.record_validation()
        (self.checkout / self.source_relative).write_text('class Adapter { val tablet = true }\n')
        manifest = capture_checkpoint(self.task)
        self.assertEqual('adaptation-in-progress', manifest['stage'])
        self.assertEqual('NEEDS_HOOK_REVIEW', json.loads((self.task / 'candidate-report.json').read_text())['status'])
        task, _ = self.fresh_task()
        self.assertTrue(restore_checkpoint(task, self.task, {'status': 'WAITING_RUNTIME'})['needsModel'])

    def test_legacy_static_stage_without_validation_binding_resumes_model(self):
        capture_checkpoint(self.task)
        manifest_path = self.task / 'checkpoint.json'
        manifest = json.loads(manifest_path.read_text())
        manifest['stage'] = 'static-verified-pending-runtime'
        manifest_path.write_text(json.dumps(manifest))
        task, _ = self.fresh_task()
        self.assertTrue(restore_checkpoint(task, self.task, {'status': 'WAITING_RUNTIME'})['needsModel'])

    def test_static_stage_on_a_changed_base_resumes_model(self):
        self.record_validation()
        capture_checkpoint(self.task)
        (self.repository / 'new-main.txt').write_text('main advanced\n')
        subprocess.run(['git', 'add', '.'], cwd=self.repository, check=True)
        subprocess.run(['git', '-c', 'user.name=Test', '-c', 'user.email=test@localhost', 'commit', '-qm', 'Advance main'], cwd=self.repository, check=True)
        task, checkout = self.fresh_task()
        state = json.loads((task / 'state.json').read_text())
        state['base'] = state['agent_base'] = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=checkout, text=True).strip()
        (task / 'state.json').write_text(json.dumps(state))
        self.assertTrue(restore_checkpoint(task, self.task, {'status': 'WAITING_RUNTIME'})['needsModel'])

    def test_checkpoint_round_trip_restores_allowed_tracked_and_new_files(self):
        self.source = self.checkout / self.source_relative
        self.source.write_text('class Adapter { val tablet = true }\n')
        new_source = self.checkout / 'app/src/main/kotlin/io/github/nku100/impad/apps/wecom/NewHook.kt'
        new_source.write_text('class NewHook\n')

        manifest = capture_checkpoint(self.task)

        self.assertEqual('wecom-com.tencent.wework-arm64-v8a-83835-' + 'a' * 64, manifest['candidateKey'])
        self.assertEqual('adaptation-in-progress', manifest['stage'])
        self.assertEqual('class Adapter { val tablet = true }\n', self.source.read_text())
        self.assertIn('NewHook.kt', (self.task / 'checkpoint.patch').read_text())

        fresh_task = self.root / 'fresh-task'
        fresh_checkout = fresh_task / 'checkout'
        subprocess.run(['git', 'clone', '-q', str(self.repository), str(fresh_checkout)], check=True)
        fresh_state = dict(self.state, worktree=str(fresh_checkout))
        fresh_task.mkdir(exist_ok=True)
        (fresh_task / 'state.json').write_text(json.dumps(fresh_state))
        context = restore_checkpoint(fresh_task, self.task, {'status': 'ADAPTATION_RUNNING'})

        self.assertTrue(context['needsModel'])
        self.assertEqual('', subprocess.check_output(['git', 'diff', '--cached', '--name-only'],
                                                     cwd=fresh_checkout, text=True).strip())
        self.assertIn(str(self.source_relative), subprocess.check_output(
            ['git', 'diff', '--name-only'], cwd=fresh_checkout, text=True))
        self.assertIn('NewHook.kt', subprocess.check_output(
            ['git', 'ls-files', '--others', '--exclude-standard'], cwd=fresh_checkout, text=True))
        self.assertEqual('class Adapter { val tablet = true }\n',
                         (fresh_checkout / self.source_relative).read_text())
        self.assertTrue((fresh_checkout / 'app/src/main/kotlin/io/github/nku100/impad/apps/wecom/NewHook.kt').is_file())

    def test_checkpoint_rejects_candidate_mismatch_and_modified_patch(self):
        capture_checkpoint(self.task)
        wrong_state = dict(self.state, report={'appId': 'wecom', 'identity': dict(self.identity,
                                                                                 versionCode=83836)})
        (self.task / 'state.json').write_text(json.dumps(wrong_state))
        with self.assertRaisesRegex(ValueError, 'candidate'):
            restore_checkpoint(self.task, self.task, {'status': 'ADAPTATION_RUNNING'})
        (self.task / 'state.json').write_text(json.dumps(self.state))
        patch = self.task / 'checkpoint.patch'
        patch.write_text(patch.read_text() + '# changed\n')
        with self.assertRaisesRegex(ValueError, 'SHA-256'):
            restore_checkpoint(self.task, self.task, {'status': 'ADAPTATION_RUNNING'})

        patch.write_bytes((self.task / 'checkpoint.patch').read_bytes())
        manifest_path = self.task / 'checkpoint.json'
        manifest = json.loads(manifest_path.read_text())
        manifest['patchSha256'] = '0' * 64
        manifest_path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, 'SHA-256'):
            restore_checkpoint(self.task, self.task, {'status': 'ADAPTATION_RUNNING'})

    def test_static_checkpoint_skips_model_unless_runtime_was_rejected(self):
        self.record_validation()
        capture_checkpoint(self.task)

        context = restore_checkpoint(self.task, self.task, {'status': 'WAITING_RUNTIME'})
        rejected = restore_checkpoint(self.task, self.task, {'status': 'RUNTIME_REJECTED'})
        owner_input = restore_checkpoint(self.task, self.task, {'status': 'WAITING_RUNTIME', 'ownerComments': [{'id': 1, 'body': 'continue'}]})

        self.assertFalse(context['needsModel'])
        self.assertTrue(rejected['needsModel'])
        self.assertTrue(owner_input['needsModel'])


if __name__ == '__main__':
    unittest.main()
