import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
NODE = os.environ.get("CODEX_TEST_NODE") or shutil.which("node")


class CodexLiveLauncherTest(unittest.TestCase):
    def _node_environment(self, temporary, app_id):
        task = Path(temporary) / 'task'
        task.mkdir()
        artifact = Path(temporary) / 'node_modules/@actions/artifact/lib/artifact.js'
        artifact.parent.mkdir(parents=True)
        (artifact.parent / 'package.json').write_text('{"type":"module"}')
        artifact.write_text('export class DefaultArtifactClient { async uploadArtifact() {} }\n')
        stub = Path(temporary) / "codex_adaptation_parse_probe.py"
        stub.write_text(
            "import argparse\n"
            "from scripts.ci import codex_adaptation\n"
            "parse_args = argparse.ArgumentParser.parse_args\n"
            "def capture_args(parser, args=None, namespace=None):\n"
            "    parsed = parse_args(parser, args, namespace)\n"
            "    print('ARGPARSE_APP_ID:' + parsed.app_id)\n"
            "    return parsed\n"
            "argparse.ArgumentParser.parse_args = capture_args\n"
            "codex_adaptation.run_model = lambda directory, prompt: None\n"
            "codex_adaptation.main()\n"
        )
        env = dict(os.environ)
        env.pop("APP_ID", None)
        if app_id is not None:
            env["APP_ID"] = app_id
        env.update({
            "CODEX_CONTROLLER_MODULE": "codex_adaptation_parse_probe",
            "CONTROLLER_ROOT": temporary,
            "CODEX_TASK_DIR": str(task),
            "ADAPTATION_PROMPT": str(Path(temporary) / "prompt.md"),
            "PYTHONPATH": os.pathsep.join((str(Path(temporary)), str(ROOT))),
            "CODEX_HOME": str(Path(temporary) / 'no-auth'),
            "CODEX_TRACE_RECIPIENT": 'not-a-real-recipient',
        })
        return env

    @staticmethod
    def _node_script():
        return r"""
import {run} from './scripts/ci/codex_live.mjs';
await run({
  info() {}, warning() {},
  setFailed(message) { console.error(message); process.exitCode = 1; }
});
"""

    @unittest.skipUnless(NODE, "Node.js is required for launcher integration tests")
    def test_node_launcher_arguments_parse_with_real_controller_for_every_app(self):
        for app_id in ("qq", "wechat", "wecom"):
            with self.subTest(app_id=app_id), tempfile.TemporaryDirectory() as temporary:
                result = subprocess.run(
                    [NODE, "--input-type=module", "-e", self._node_script()],
                    cwd=ROOT, env=self._node_environment(temporary, app_id),
                    text=True, capture_output=True, check=False,
                )
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertIn(f"ARGPARSE_APP_ID:{app_id}", result.stdout)

    @unittest.skipUnless(NODE, "Node.js is required for launcher integration tests")
    def test_node_launcher_rejects_missing_app_id_before_python_spawn(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = subprocess.run(
                [NODE, "--input-type=module", "-e", self._node_script()],
                cwd=ROOT, env=self._node_environment(temporary, None),
                text=True, capture_output=True, check=False,
            )
            self.assertNotEqual(0, result.returncode)
            self.assertIn("APP_ID must be one of qq, wechat, or wecom", result.stderr)
            self.assertNotIn("usage: codex_adaptation.py", result.stderr)

    @unittest.skipUnless(NODE, "Node.js is required for launcher integration tests")
    def test_node_launcher_rejects_unregistered_app_id_before_python_spawn(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = subprocess.run(
                [NODE, "--input-type=module", "-e", self._node_script()],
                cwd=ROOT, env=self._node_environment(temporary, "telegram"),
                text=True, capture_output=True, check=False,
            )
            self.assertNotEqual(0, result.returncode)
            self.assertIn("APP_ID must be one of qq, wechat, or wecom", result.stderr)
            self.assertNotIn("usage: codex_adaptation.py", result.stderr)


if __name__ == "__main__":
    unittest.main()
