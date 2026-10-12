import json
import subprocess
import tempfile
import unittest
from pathlib import Path

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
            'report': {'appId': 'wecom', 'identity': self.identity},
        }
        (self.task / 'state.json').write_text(json.dumps(self.state))
        self.checkpoint_dir = self.root / 'checkpoint'
        (self.task / 'resume-context.json').write_text(json.dumps({'status': 'ADAPTATION_RUNNING'}))

    def tearDown(self):
        self.temp.cleanup()

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
        report = self.task / 'candidate-report.json'
        report.write_text(json.dumps({'status': 'STATIC_VERIFIED_PENDING_RUNTIME'}))
        (self.task / 'adaptation.patch').write_bytes(b'')
        capture_checkpoint(self.task)

        context = restore_checkpoint(self.task, self.task, {'status': 'WAITING_RUNTIME'})
        rejected = restore_checkpoint(self.task, self.task, {'status': 'RUNTIME_REJECTED'})

        self.assertFalse(context['needsModel'])
        self.assertTrue(rejected['needsModel'])


if __name__ == '__main__':
    unittest.main()
