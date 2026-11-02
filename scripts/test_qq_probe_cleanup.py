import subprocess
from types import SimpleNamespace
import unittest
from scripts.ci.apps.qq.runtime_probe import installed_identity


class QqProbeCleanupTest(unittest.TestCase):
    def test_installed_identity_hashes_base_and_every_split(self):
        commands = []

        def adb(*args, **kwargs):
            commands.append(args)
            if args[:3] == ('shell', 'pm', 'path'):
                return subprocess.CompletedProcess(args, 0, b'package:/data/app/pkg/base.apk\npackage:/data/app/pkg/split_config.apk\n', b'')
            path = args[-1]
            digest = 'a' * 64 if path.endswith('base.apk') else 'b' * 64
            return subprocess.CompletedProcess(args, 0, f'{digest}  {path}\n'.encode(), b'')

        identity = installed_identity(SimpleNamespace(adb=adb), 'example.pkg')
        self.assertEqual([
            {'path': '/data/app/pkg/split_config.apk', 'sha256': 'b' * 64}
        ], identity['splitApks'])
        self.assertEqual(3, len(commands))

if __name__=='__main__':unittest.main()
