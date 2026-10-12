import copy
import unittest
import io
from contextlib import redirect_stderr
import hashlib
import json
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

from scripts.ci.candidate_selection import replay_report, write_local_runtime_assets, main


class CandidateSelectionTest(unittest.TestCase):
    def setUp(self):
        self.profile = {'identity': {'packageName': 'com.tencent.mm', 'versionName': '8.0.78', 'versionCode': 3180,
                                     'abi': 'arm64-v8a', 'apkSha256': 'a' * 64,
                                     'signerSha256': '0fe4ff85c215918396dadc7cd8ce6963339af33d37751a56e54c7206b63a3c7c'},
                        'sourceUrl': 'https://dldir1v6.qq.com/weixin/android/weixin8078android3180_arm64.apk',
                        'hooks': [{'id': 'tablet'}, {'id': 'login'}],
                        'verificationStatus': 'runtime-verified-local'}

    def test_cli_rejects_explicit_version_selection(self):
        error = io.StringIO()
        with redirect_stderr(error), patch('sys.argv', ['candidate_selection', '--version', '8.0.78', '--directory', '/tmp/unused']):
            with self.assertRaises(SystemExit) as failure:
                main()
        self.assertEqual(failure.exception.code, 2)
        self.assertIn("unrecognized arguments: --version", error.getvalue())

    def test_replay_requires_an_exact_registered_artifact(self):
        report = replay_report([self.profile], self.profile['sourceUrl'], 'a' * 64)
        self.assertEqual(report['checkedVersions'], ['8.0.78'])
        self.assertEqual(report['status'], 'STATIC_VERIFIED_PENDING_RUNTIME')
        self.assertEqual(self.profile['verificationStatus'], 'runtime-verified-local')
        with self.assertRaisesRegex(ValueError, 'not registered'):
            replay_report([self.profile], self.profile['sourceUrl'], 'b' * 64)

    def test_local_runtime_assets_follow_the_selected_application_profile(self):
        target = json.loads(Path('compatibility/wecom/targets.json').read_text())[0]
        original = copy.deepcopy(target)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_local_runtime_assets(target, 'wecom', root)
            local_profile = json.loads((root / 'assets/compatibility-local/wecom/targets.json').read_text())[0]
            scope = (root / 'resources/META-INF/xposed/scope.list').read_text().splitlines()
        self.assertIsNone(local_profile['sourceUrl'])
        self.assertEqual(local_profile['identity'], target['identity'])
        self.assertEqual(scope, ['com.tencent.wework'])
        self.assertEqual(target, original)

    def test_registered_wecom_replay_selects_its_single_target_without_a_sha_override(self):
        target = json.loads(Path('compatibility/wecom/targets.json').read_text())[0]
        body = b'cached registered WeCom baseline'
        digest = hashlib.sha256(body).hexdigest()
        target['identity']['apkSha256'] = digest
        target['sourceUrl'] = 'https://dldir1.qq.com/wework/work_weixin/wecom-test.apk'
        remote = {'content_length': len(body), 'last_modified': 'Wed, 07 Oct 2026 00:00:00 GMT', 'etag': 'wecom-test'}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = root / 'targets.json'
            manifest.write_text(json.dumps([target]))
            cache = root / 'inputs/cache'
            cache.mkdir(parents=True)
            (cache / f'{digest}.apk').write_bytes(body)
            (cache / 'candidate.json').write_text(json.dumps(dict(remote, url=target['sourceUrl'], sha256=digest)))
            with patch('sys.argv', ['candidate_selection', '--app-id', 'wecom', '--mode', 'registered',
                                    '--targets', str(manifest), '--directory', str(root / 'inputs')]), \
                    patch('scripts.ci.candidate_selection.remote_metadata', return_value=remote):
                main()
            report = json.loads((root / 'inputs/candidate-report.json').read_text())
            local_profile = json.loads((root / 'inputs/assets/compatibility-local/wecom/targets.json').read_text())[0]
            scope = (root / 'inputs/resources/META-INF/xposed/scope.list').read_text()
        self.assertEqual(report['identity']['packageName'], 'com.tencent.wework')
        self.assertEqual(report['selectionMode'], 'registered')
        self.assertEqual(local_profile['identity']['apkSha256'], digest)
        self.assertIsNone(local_profile['sourceUrl'])
        self.assertEqual(scope, 'com.tencent.wework\n')

    def test_static_only_profile_cannot_be_replayed_as_supported(self):
        profile = copy.deepcopy(self.profile)
        profile['verificationStatus'] = 'static-verified'
        with self.assertRaisesRegex(ValueError, 'runtime-verified'):
            replay_report([profile], profile['sourceUrl'], 'a' * 64)

    def test_latest_unknown_build_does_not_fall_back_to_registered_version(self):
        latest = 'https://dldir1v6.qq.com/weixin/android/weixin8079android3200_arm64.apk'
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = root / 'targets.json'
            manifest.write_text(json.dumps([self.profile]))
            unknown_apk = root / 'unknown.apk'
            unknown_apk.write_bytes(b'new official repack')
            digest = hashlib.sha256(unknown_apk.read_bytes()).hexdigest()
            with patch('sys.argv', ['candidate_selection', '--targets', str(manifest), '--directory', str(root / 'inputs')]), patch('scripts.ci.apps.wechat.discovery_policy.fetch_official_page', return_value=latest), patch('scripts.ci.candidate_selection.remote_metadata', return_value={}), patch('scripts.ci.candidate_selection.download_candidate', return_value=(unknown_apk, digest, {}, {})):
                with self.assertRaisesRegex(ValueError, 'Latest official APK is not runtime-verified'):
                    main()
            self.assertFalse(list((root / 'inputs').glob('wechat-*.apk')))

    def test_registered_mode_selects_highest_runtime_verified_profile_without_discovery(self):
        newer = copy.deepcopy(self.profile)
        newer['identity'].update(versionName='8.0.79', versionCode=3190)
        newer['sourceUrl'] = 'https://dldir1v6.qq.com/weixin/android/weixin8079android3190_arm64.apk'
        static_newest = copy.deepcopy(newer)
        static_newest['identity'].update(versionName='8.0.80', versionCode=3200)
        static_newest['sourceUrl'] = 'https://dldir1v6.qq.com/weixin/android/weixin8080android3200_arm64.apk'
        static_newest['verificationStatus'] = 'static-verified'
        body = b'cached official registered artifact'
        digest = hashlib.sha256(body).hexdigest()
        newer['identity']['apkSha256'] = digest
        static_newest['identity']['apkSha256'] = digest
        remote = {'content_length': len(body), 'last_modified': 'Wed, 07 Oct 2026 00:00:00 GMT', 'etag': ''}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = root / 'targets.json'
            original_targets = [self.profile, newer, static_newest]
            manifest.write_text(json.dumps(original_targets))
            cache = root / 'inputs/cache'
            cache.mkdir(parents=True)
            (cache / f'{digest}.apk').write_bytes(body)
            (cache / 'candidate.json').write_text(json.dumps(dict(remote, url=newer['sourceUrl'], sha256=digest)))
            with patch('sys.argv', ['candidate_selection', '--mode', 'registered', '--targets', str(manifest), '--directory', str(root / 'inputs')]), patch('scripts.ci.apps.wechat.discovery_policy.discover', side_effect=AssertionError('discovery must not run')), patch('scripts.ci.candidate_selection.remote_metadata', return_value=remote):
                main()
            report = json.loads((root / 'inputs/candidate-report.json').read_text())
            self.assertEqual(report['selectionMode'], 'registered')
            self.assertEqual(report['identity']['versionCode'], 3190)
            self.assertEqual(report['identity']['apkSha256'], digest)
            self.assertEqual((root / f'inputs/wechat-{digest}.apk').read_bytes(), body)
            self.assertEqual(json.loads(manifest.read_text()), original_targets)
            local_profile = json.loads((root / 'inputs/assets/compatibility-local/wechat/targets.json').read_text())[0]
            self.assertIsNone(local_profile['sourceUrl'])
            self.assertEqual((root / 'inputs/resources/META-INF/xposed/scope.list').read_text(), 'com.tencent.mm\n')

    def test_registered_mode_rejects_static_only_policy(self):
        profile = copy.deepcopy(self.profile)
        profile['verificationStatus'] = 'static-verified'
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = root / 'targets.json'
            manifest.write_text(json.dumps([profile]))
            with patch('sys.argv', ['candidate_selection', '--mode', 'registered', '--targets', str(manifest), '--directory', str(root / 'inputs')]), patch('scripts.ci.apps.wechat.discovery_policy.discover', side_effect=AssertionError('discovery must not run')):
                with self.assertRaisesRegex(ValueError, 'No runtime-verified registered APK'):
                    main()

    def test_registered_mode_requires_digest_for_multiple_latest_version_variants(self):
        repack = copy.deepcopy(self.profile)
        repack['identity']['apkSha256'] = 'b' * 64
        repack['sourceUrl'] = 'https://dldir1v6.qq.com/weixin/android/repack.apk'
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = root / 'targets.json'
            manifest.write_text(json.dumps([self.profile, repack]))
            with patch('sys.argv', ['candidate_selection', '--mode', 'registered', '--targets', str(manifest), '--directory', str(root / 'inputs')]), patch('scripts.ci.apps.wechat.discovery_policy.discover', side_effect=AssertionError('discovery must not run')):
                with self.assertRaisesRegex(ValueError, 'specify --apk-sha256'):
                    main()

    def test_registered_mode_uses_explicit_digest_for_latest_version_variant(self):
        body = b'cached repack bytes'
        digest = hashlib.sha256(body).hexdigest()
        repack = copy.deepcopy(self.profile)
        repack['identity']['apkSha256'] = digest
        repack['sourceUrl'] = 'https://dldir1v6.qq.com/weixin/android/repack.apk'
        original = copy.deepcopy(self.profile)
        original['identity']['apkSha256'] = 'c' * 64
        remote = {'content_length': len(body), 'last_modified': 'Wed, 07 Oct 2026 00:00:00 GMT', 'etag': ''}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = root / 'targets.json'
            manifest.write_text(json.dumps([original, repack]))
            cache = root / 'inputs/cache'
            cache.mkdir(parents=True)
            (cache / f'{digest}.apk').write_bytes(body)
            (cache / 'candidate.json').write_text(json.dumps(dict(remote, url=repack['sourceUrl'], sha256=digest)))
            with patch('sys.argv', ['candidate_selection', '--mode', 'registered', '--apk-sha256', digest, '--targets', str(manifest), '--directory', str(root / 'inputs')]), patch('scripts.ci.apps.wechat.discovery_policy.discover', side_effect=AssertionError('discovery must not run')), patch('scripts.ci.candidate_selection.remote_metadata', return_value=remote):
                main()
            report = json.loads((root / 'inputs/candidate-report.json').read_text())
            self.assertEqual(digest, report['identity']['apkSha256'])
            self.assertEqual(body, (root / f'inputs/wechat-{digest}.apk').read_bytes())

    def test_registered_mode_rejects_download_with_wrong_digest(self):
        profile = copy.deepcopy(self.profile)
        body = b'APK bytes that do not match the registered digest'
        headers = {'Content-Length': str(len(body)), 'Last-Modified': 'Wed, 07 Oct 2026 00:00:00 GMT', 'ETag': ''}
        response = Mock(status=200, headers=Mock(get=Mock(side_effect=lambda key, default=None: headers.get(key, default))), geturl=lambda: profile['sourceUrl'])
        response.read.side_effect = [body, b'']
        context = Mock()
        context.__enter__ = Mock(return_value=response)
        context.__exit__ = Mock(return_value=False)
        remote = {'content_length': len(body), 'last_modified': headers['Last-Modified'], 'etag': ''}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = root / 'targets.json'
            manifest.write_text(json.dumps([profile]))
            with patch('sys.argv', ['candidate_selection', '--mode', 'registered', '--targets', str(manifest), '--directory', str(root / 'inputs')]), patch('scripts.ci.apps.wechat.discovery_policy.discover', side_effect=AssertionError('discovery must not run')), patch('scripts.ci.candidate_selection.remote_metadata', return_value=remote), patch('scripts.ci.fetch_latest_wechat_apk.urlopen', return_value=context):
                with self.assertRaisesRegex(ValueError, 'APK SHA-256 mismatch'):
                    main()
            self.assertFalse(list((root / 'inputs').glob('wechat-*.apk')))

    def test_replay_cli_reuses_verified_cache_and_writes_inputs(self):
        body = b'cached official artifact'
        digest = hashlib.sha256(body).hexdigest()
        profile = copy.deepcopy(self.profile)
        profile['identity']['apkSha256'] = digest
        remote = {'content_length': len(body), 'last_modified': 'Wed, 07 Oct 2026 00:00:00 GMT', 'etag': ''}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = root / 'targets.json'
            manifest.write_text(json.dumps([profile]))
            cache = root / 'cache'
            cache.mkdir()
            (cache / (digest + '.apk')).write_bytes(body)
            (cache / 'candidate.json').write_text(json.dumps(dict(remote, url=profile['sourceUrl'], sha256=digest)))
            with patch('sys.argv', ['candidate_selection', '--targets', str(manifest), '--directory', str(root)]), patch('scripts.ci.apps.wechat.discovery_policy.fetch_official_page', return_value=profile['sourceUrl']), patch('scripts.ci.candidate_selection.remote_metadata', return_value=remote):
                main()
            self.assertEqual((root / f'wechat-{digest}.apk').read_bytes(), body)
            self.assertEqual(json.loads((root / 'candidate-report.json').read_text())['identity']['versionName'], '8.0.78')
