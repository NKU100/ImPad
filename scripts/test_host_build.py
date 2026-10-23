import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


class HostBuildTest(unittest.TestCase):
    def test_managed_build_rejects_caller_supplied_gradle_tasks(self):
        script = Path('scripts/ci/host-build.sh').resolve()
        result = subprocess.run(['bash', str(script), ':app:testReleaseUnitTest'],
                                capture_output=True, text=True)
        self.assertEqual(2, result.returncode)
        self.assertIn('does not accept arguments', result.stderr)

    def test_build_script_changes_are_rejected_before_host_execution(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); task = root / 'task'; task.mkdir()
            workspace = root / 'checkout'; workspace.mkdir()
            subprocess.run(['git','init','-q',str(workspace)],check=True)
            script = workspace / 'gradlew'; script.write_text('#!/bin/sh\nexit 0\n'); script.chmod(0o755)
            subprocess.run(['git','add','.'],cwd=workspace,check=True)
            subprocess.run(['git','-c','user.name=Test','-c','user.email=test@localhost','commit','-qm','seed'],cwd=workspace,check=True)
            base = subprocess.check_output(['git','rev-parse','HEAD'],cwd=workspace,text=True).strip()
            (task/'state.json').write_text(json.dumps({'appId':'wechat','worktree':str(workspace),'base':base,'agent_base':base,'existing_tests':{},'before':[],'report':{},'apks':[]}))
            script.write_text('#!/bin/sh\ntouch host-command-ran\n')
            result = subprocess.run(['python3',str(Path('scripts/ci/host_build.py').resolve()),str(task)],capture_output=True,text=True,
                                    env=dict(os.environ,CODEX_HOME=str(root/'empty-auth')))
            self.assertEqual(1,result.returncode)
            self.assertIn('outside the allowed adaptation paths',result.stderr)
            self.assertFalse((workspace/'host-command-ran').exists())


if __name__ == '__main__':
    unittest.main()
