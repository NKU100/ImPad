import hashlib
from pathlib import Path
import tempfile
import unittest
from scripts.ci.redroid_environment_probe import checked_digest


class ReDroidEnvironmentProbeTest(unittest.TestCase):
    def test_rejects_modified_framework_asset(self):
        with tempfile.TemporaryDirectory() as directory:
            asset = Path(directory) / 'framework.zip'
            asset.write_bytes(b'pinned framework')
            digest = hashlib.sha256(asset.read_bytes()).hexdigest()
            self.assertEqual(digest, checked_digest(asset, digest))
            asset.write_bytes(b'replaced framework')
            with self.assertRaisesRegex(ValueError, 'Asset hash mismatch'):
                checked_digest(asset, digest)

    def test_copied_init_rc_has_android_init_mode_independent_of_umask(self):
        from scripts.ci.redroid_environment_probe import Probe

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            template = root / 'template.rc'
            copied = root / 'context/system/etc/init/impad-magisk.rc'
            copied.parent.mkdir(parents=True)
            template.write_text('service test /system/bin/test\n')
            template.chmod(0o640)
            template_mode = template.stat().st_mode & 0o777
            sibling = copied.parent / 'existing.conf'
            sibling.write_text('preserve')
            sibling.chmod(0o600)

            old_umask = __import__('os').umask(0o002)
            try:
                Probe.copy_init_rc(template, copied)
            finally:
                __import__('os').umask(old_umask)

            self.assertEqual(copied.stat().st_mode & 0o777, 0o644)
            self.assertEqual(template.stat().st_mode & 0o777, template_mode)
            self.assertEqual(sibling.stat().st_mode & 0o777, 0o600)
            self.assertEqual(copied.read_bytes(), template.read_bytes())


if __name__ == '__main__':
    unittest.main()

class BootReconnectTest(unittest.TestCase):
    def test_retries_when_root_restart_temporarily_offlines_transport(self):
        from unittest.mock import patch
        import subprocess
        from scripts.ci.redroid_environment_probe import Probe
        with tempfile.TemporaryDirectory() as directory:
            probe = Probe(Path(directory))
            properties = iter([
                subprocess.CompletedProcess([], 1, b'', b'adb: device offline'),
                subprocess.CompletedProcess([], 0, b'properties', b'')])
            seen = []
            def shell(command, name, **kwargs):
                seen.append(name)
                if command == 'getprop sys.boot_completed':
                    return subprocess.CompletedProcess([], 0, b'1', b'')
                if command == 'getprop; ps -A':
                    return next(properties)
                raise AssertionError(command)
            with patch.object(probe, 'run'), patch.object(probe, 'adb', return_value=subprocess.CompletedProcess([], 0, b'', b'')), patch.object(probe, 'shell', side_effect=shell), patch('scripts.ci.redroid_environment_probe.time.sleep'):
                probe.boot('framework')
            self.assertEqual(seen.count('framework-properties'), 2)

    def test_reconnects_after_root_command_closes_the_transport(self):
        from unittest.mock import patch
        import subprocess
        from scripts.ci.redroid_environment_probe import Probe
        with tempfile.TemporaryDirectory() as directory:
            probe = Probe(Path(directory))
            roots = iter([subprocess.CompletedProcess([], 1, b'', b'closed'), subprocess.CompletedProcess([], 0, b'already root', b'')])
            def adb(args, name, **kwargs):
                if args == ['root']:
                    result = next(roots)
                    if kwargs.get('check', True) and result.returncode:
                        raise RuntimeError('root connection closed')
                    return result
                return subprocess.CompletedProcess([], 0, b'', b'')
            def shell(command, name, **kwargs):
                return subprocess.CompletedProcess([], 0, b'1' if command == 'getprop sys.boot_completed' else b'properties', b'')
            with patch.object(probe, 'run') as run, patch.object(probe, 'adb', side_effect=adb), patch.object(probe, 'shell', side_effect=shell), patch('scripts.ci.redroid_environment_probe.time.sleep'):
                probe.boot('magisk')
            self.assertGreaterEqual(run.call_count, 2)

class LifecycleTest(unittest.TestCase):
    def test_failed_framework_setup_always_cleans_up_and_saves_failure(self):
        from unittest.mock import patch
        import json
        from scripts.ci.redroid_environment_probe import Probe
        with tempfile.TemporaryDirectory() as directory:
            probe = Probe(Path(directory))
            with patch.object(probe, 'setup', side_effect=RuntimeError('Binder unavailable')), patch.object(probe, 'cleanup') as cleanup:
                self.assertFalse(probe.execute())
            cleanup.assert_called_once()
            report = json.loads((probe.output / 'report.json').read_text())
            self.assertEqual(report['status'], 'FAILED')
            self.assertIn('Binder unavailable', report['reason'])

    def test_stock_boot_failure_preserves_candidate_unavailable_report(self):
        from unittest.mock import patch
        import json
        from scripts.ci.redroid_environment_probe import Probe
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            identity = {
                'packageName': 'com.tencent.mobileqq', 'versionName': '9.3.1',
                'versionCode': 14378, 'abi': 'arm64-v8a', 'apkSha256': 'a' * 64,
                'signerSha256': 'ea6e97ad6c34f7039a9c6daba732c97d0e098e83ede2b4d52c76eb0184ac7a38',
            }
            candidate = {
                'schemaVersion': 1, 'appId': 'qq', 'status': 'PREPARED',
                'identity': identity,
                'sourceUrl': 'https://downv6.qq.com/qqweb/QQ_1/android_apk/9.3.1.apk',
                'apkFiles': [{'path': 'qq.apk', 'role': 'base', 'sha256': identity['apkSha256']}],
                'expectedPlan': {'selection': 'UNIQUE', 'ruleId': 'qq_device_type_001',
                                 'strategyId': 'qq_tablet_001', 'targets': {}},
            }
            (root / 'candidate-report.json').write_text(json.dumps(candidate))
            (root / 'module').mkdir()
            (root / 'module/app-debug.apk').write_bytes(b'module')
            probe = Probe(root)
            with patch.object(probe, 'setup', side_effect=RuntimeError('Stock boot timed out')), patch.object(probe, 'cleanup'):
                self.assertFalse(probe.execute(root))
            report = json.loads((root / 'evidence/smoke-report.json').read_text())
            self.assertEqual(report['status'], 'ACCEPTANCE_UNAVAILABLE')
            self.assertEqual(report['apkSha256'], identity['apkSha256'])
            self.assertEqual(report['moduleApkSha256'], hashlib.sha256(b'module').hexdigest())

    def test_diagnostic_timeout_does_not_skip_container_removal(self):
        from unittest.mock import patch
        import subprocess
        from scripts.ci.redroid_environment_probe import Probe
        with tempfile.TemporaryDirectory() as directory:
            probe = Probe(Path(directory))
            commands = []
            def run(args, name, **kwargs):
                commands.append(name)
                if name == 'container-log':
                    raise subprocess.TimeoutExpired(args, 60)
            with patch.object(probe, 'run', side_effect=run), patch.object(probe, 'adb'):
                probe.cleanup()
            self.assertIn('cleanup', commands)
            self.assertTrue(probe.report['cleanupErrors'])
