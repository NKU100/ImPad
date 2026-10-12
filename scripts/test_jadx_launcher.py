import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


@unittest.skipUnless(shutil.which('flock'), 'Requires Linux flock')
class JadxLauncherTest(unittest.TestCase):
    def test_concurrent_launches_are_serial_and_heap_is_capped(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            launcher = root / 'jadx'
            shutil.copyfile('scripts/ci/jadx-serial.sh', launcher)
            launcher.chmod(0o755)
            real = root / 'jadx.real'
            real.write_text('''#!/usr/bin/env python3
import json, os, time
from pathlib import Path
active = Path('active')
active.touch(exist_ok=False)
with open('events', 'a') as log:
    log.write(json.dumps({'event':'start', 'heap':os.environ['JAVA_OPTS'], 'extra':os.environ['JADX_OPTS']})+'\\n')
time.sleep(0.2)
with open('events', 'a') as log:
    log.write(json.dumps({'event':'end'})+'\\n')
active.unlink()
''')
            real.chmod(0o755)
            environment = dict(os.environ, IMPAD_JADX_LOCK=str(root / 'lock'),
                               APP_ID='wechat', JAVA_OPTS='-Xmx99g', JADX_OPTS='-Xmx99g')
            children = [subprocess.Popen([str(launcher), '--help'], cwd=root, env=environment)
                        for _ in range(2)]
            self.assertEqual([0, 0], [child.wait(timeout=10) for child in children])
            events = [json.loads(line) for line in (root / 'events').read_text().splitlines()]
            self.assertEqual(['start', 'end', 'start', 'end'], [event['event'] for event in events])
            for event in events[::2]:
                self.assertEqual('-Xms256m -Xmx4g', event['heap'])
                self.assertEqual('', event['extra'])

    def test_wecom_jadx_heap_is_capped_to_reduce_peak_host_memory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            launcher = root / 'jadx'
            shutil.copyfile('scripts/ci/jadx-serial.sh', launcher)
            launcher.chmod(0o755)
            real = root / 'jadx.real'
            real.write_text('#!/usr/bin/env python3\nimport os\nprint(os.environ["JAVA_OPTS"])\n')
            real.chmod(0o755)
            environment = dict(os.environ, IMPAD_JADX_LOCK=str(root / 'lock'), APP_ID='wecom')
            result = subprocess.run([str(launcher), '--help'], cwd=root, env=environment,
                                    capture_output=True, text=True, check=True)
            self.assertEqual('-Xms256m -Xmx2g\n', result.stdout)

    def test_unconfigured_lock_fails_before_launch(self):
        environment = dict(os.environ)
        environment.pop('IMPAD_JADX_LOCK', None)
        result = subprocess.run(['bash', 'scripts/ci/jadx-serial.sh', '--help'], env=environment,
                                capture_output=True)
        self.assertNotEqual(0, result.returncode)
        self.assertIn(b'lock must be configured', result.stderr)
