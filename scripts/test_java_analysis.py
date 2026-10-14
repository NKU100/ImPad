import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


class JavaAnalysisTest(unittest.TestCase):
    def test_jadx_calls_share_lock_and_bound_heap(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            real = root / 'java.real'
            real.write_text('''#!/usr/bin/env python3
import json,sys,time
from pathlib import Path
p=Path('active');p.touch(exist_ok=False)
with open('events','a') as f:f.write(json.dumps(sys.argv[1:])+'\\n')
time.sleep(.2)
p.unlink()
''')
            real.chmod(0o755)
            env = dict(os.environ, IMPAD_REAL_JAVA=str(real), IMPAD_JAVA_LOCK=str(root / 'lock'))
            command = ['python3', str(Path('scripts/ci/java-analysis.py').resolve()), '-Xmx8g', '-XX:MaxHeapSize=8g', '-cp', 'jadx/lib/*', 'Inspect']
            children = [subprocess.Popen(command, cwd=root, env=env) for _ in range(2)]
            self.assertEqual([0, 0], [child.wait(timeout=10) for child in children])
            events = [json.loads(line) for line in (root/'events').read_text().splitlines()]
            self.assertEqual(2, len(events))
            for args in events:
                self.assertEqual('-Xmx1536m', args[0])
                self.assertNotIn('-Xmx8g', args)
                self.assertNotIn('-XX:MaxHeapSize=8g', args)
                self.assertEqual(['-cp', 'jadx/lib/*', 'Inspect'], args[1:])

    def test_non_jadx_java_preserves_arguments(self):
        with tempfile.TemporaryDirectory() as temporary:
            real=Path(temporary)/'java'
            real.write_text('#!/usr/bin/env python3\nimport json,sys\nprint(json.dumps(sys.argv[1:]))\n')
            real.chmod(0o755)
            result=subprocess.run(['python3','scripts/ci/java-analysis.py','-Xmx4g','-version'],
                env=dict(os.environ,IMPAD_REAL_JAVA=str(real)),capture_output=True,text=True,check=True)
            self.assertEqual(['-Xmx4g','-version'],json.loads(result.stdout))

    def test_shadow_java_home_routes_java_and_preserves_jdk(self):
        from scripts.ci.codex_sandbox import prepare_qq_java
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);jdk=root/'jdk';(jdk/'bin').mkdir(parents=True)
            for name in ('java','javac'):(jdk/'bin'/name).write_text(name)
            (jdk/'release').write_text('jdk release')
            env={'JAVA_HOME':str(jdk),'IMPAD_JADX_LOCK':str(root/'jadx.lock')}
            prepare_qq_java(root/'tools',env)
            shadow=Path(env['JAVA_HOME'])
            self.assertEqual(str(jdk.resolve()/'bin/java'),env['IMPAD_REAL_JAVA'])
            self.assertEqual('javac',(shadow/'bin/javac').read_text())
            self.assertEqual('jdk release',(shadow/'release').read_text())
            self.assertIn('Serialize JADX',(shadow/'bin/java').read_text())
            self.assertNotEqual(env['IMPAD_JADX_LOCK'],env['IMPAD_JAVA_LOCK'])
            prepare_qq_java(root/'tools',env)
