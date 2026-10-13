import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts.ci.apps.qq.runtime_probe import observe


class QqProbeCleanupTest(unittest.TestCase):
    def test_forward_failure_stops_observer_server(self):
        self.check_failure('900', 'forward failed')

    def test_unparseable_startup_response_recovers_pid_for_cleanup(self):
        self.check_failure('invalid', 'PID')

    def check_failure(self, startup_output, expected_error):
        commands = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'qq.apk').write_bytes(b'exact APK fixture')
            digest = hashlib.sha256((root / 'qq.apk').read_bytes()).hexdigest()
            def result(text=''):
                return subprocess.CompletedProcess([], 0, text.encode(), b'')

            def device(*args, **kwargs):
                commands.append(args)
                if args == ('shell', 'pm', 'path', 'com.tencent.mobileqq'):
                    return result('package:/data/app/qq/base.apk')
                if args[:2] == ('shell', 'sha256sum'):
                    return result(digest + ' /data/app/qq/base.apk')
                if args[:2] == ('shell', 'pidof'):
                    started = any(c[:4] == ('shell', 'am', 'start', '-n') for c in commands if isinstance(c, tuple))
                    return result('200' if started else '')
                if args[0] == 'forward':
                    raise RuntimeError('forward failed')
                return result()

            def superuser(command, **kwargs):
                commands.append(command)
                if command.startswith('cat '):
                    return result('900')
                if 'echo $!' in command:
                    return result(startup_output)
                return result()

            smoke = SimpleNamespace(ROOT=root, EVIDENCE=root, MODULE='fixture.module',
                                    configure_manager=lambda enabled: None,
                                    UI_STRATEGY=SimpleNamespace(launcher_component='fixture.activity'),
                                    APP_PACKAGE='com.tencent.mobileqq',
                                    apk_digest=lambda apk: hashlib.sha256(apk.read_bytes()).hexdigest(),
                                    adb=device, su=superuser, run=lambda *a, **k: result())
            with patch('scripts.ci.apps.qq.runtime_probe.manager_state', return_value={'moduleEnabled': False, 'scopeVerified': True}):
                with self.assertRaisesRegex(RuntimeError, expected_error):
                    observe(smoke, False)
        self.assertIn('kill 900', commands)


if __name__ == '__main__':
    unittest.main()
