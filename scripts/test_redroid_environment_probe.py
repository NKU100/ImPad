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
            with patch.object(probe, 'run'), patch.object(probe, 'adb'), patch.object(probe, 'shell', side_effect=shell), patch('scripts.ci.redroid_environment_probe.time.sleep'):
                probe.boot('framework')
            self.assertEqual(seen.count('framework-properties'), 2)
