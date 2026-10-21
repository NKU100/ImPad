import platform
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(platform.system() == 'Linux' and shutil.which('cc'),
                     'QQ collector owner harness requires Linux C toolchain')
class CollectorOwnerTest(unittest.TestCase):
    def test_bounded_owner_and_pending_payload_contract(self):
        source = ROOT / 'scripts/ci/apps/qq/probe/collector_owner_test.c'
        with tempfile.TemporaryDirectory(prefix='impad-qq-owner-test-') as tmp:
            binary = Path(tmp) / 'collector-owner-test'
            compile_result = subprocess.run(
                ['cc', '-std=c11', '-Wall', '-Wextra', '-Werror',
                 '-Wno-unused-function', '-Wno-format-truncation', str(source), '-o', str(binary)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, check=False,
            )
            self.assertEqual(compile_result.returncode, 0,
                             compile_result.stdout + compile_result.stderr)
            run = subprocess.run([str(binary)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
                                 check=False, timeout=15)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            self.assertIn('collector-owner-tests: PASS', run.stdout)
            self.assertGreaterEqual(run.stderr.count('OWNER_SCAN '), 9, run.stderr)
            self.assertIn('phase=test-transient attempt=3', run.stderr)
            self.assertIn('phase=test-unknown attempt=3', run.stderr)
            self.assertIn('phase=test-foreign-owner attempt=1', run.stderr)
            self.assertIn('foreignName=com.tencent.mobileqq:worker_fake', run.stderr)


if __name__ == '__main__':
    unittest.main()
