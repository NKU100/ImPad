import os
import json
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

    @unittest.skipUnless(NODE and shutil.which("age-keygen"), "Node.js and age are required")
    def test_early_auth_failure_uploads_only_safe_goal_diagnostics_in_live_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            task = Path(temporary) / "task"
            task.mkdir()
            artifact_module = Path(temporary) / "node_modules/@actions/artifact/lib/artifact.js"
            artifact_module.parent.mkdir(parents=True)
            (artifact_module.parent / "package.json").write_text('{"type":"module"}')
            artifact_module.write_text(
                "import {copyFile, mkdir, writeFile} from 'node:fs/promises';\n"
                "import path from 'node:path';\n"
                "export class DefaultArtifactClient {\n"
                "  async uploadArtifact(name, files, root) {\n"
                "    const destination = path.join(process.env.CODEX_TEST_ARTIFACTS, name);\n"
                "    await mkdir(destination, {recursive: true});\n"
                "    const uploaded = [];\n"
                "    for (const file of files) {\n"
                "      const relative = path.relative(root, file);\n"
                "      const target = path.join(destination, relative);\n"
                "      await mkdir(path.dirname(target), {recursive: true});\n"
                "      await copyFile(file, target);\n"
                "      uploaded.push(relative);\n"
                "    }\n"
                "    await writeFile(path.join(destination, 'manifest.json'), JSON.stringify(uploaded));\n"
                "  }\n"
                "}\n"
            )
            identity = Path(temporary) / "age-identity.txt"
            subprocess.run(["age-keygen", "-o", str(identity)], check=True,
                           capture_output=True, text=True)
            recipient = subprocess.check_output(
                ["age-keygen", "-y", str(identity)], text=True
            ).strip()
            controller_module = Path(temporary) / "codex_live_auth_failure_probe.py"
            controller_module.write_text(
                "import os\n"
                "import json\n"
                "from pathlib import Path\n"
                "from unittest.mock import patch\n"
                "from scripts.ci import codex_goal\n"
                "class ProbeAppServer:\n"
                "    def __init__(self, directory, env):\n"
                "        self.messages = [{'text': 'MODEL_MESSAGE_PRIVATE_SENTINEL'}]\n"
                "        self.thread_id = 'THREAD_PRIVATE_SENTINEL'\n"
                "        self.secrets = []\n"
                "    def request(self, method, params):\n"
                "        assert method == 'account/read'\n"
                "        assert params == {'refreshToken': True}\n"
                "        return {'account': {'type': 'ACCOUNT_TYPE_PRIVATE_SENTINEL',\n"
                "                'email': 'EMAIL_PRIVATE_SENTINEL',\n"
                "                'token': 'TOKEN_PRIVATE_SENTINEL'},\n"
                "                'requiresOpenaiAuth': True,\n"
                "                'extra': 'RESPONSE_PRIVATE_SENTINEL'}\n"
                "    def close(self):\n"
                "        pass\n"
                "with patch.object(codex_goal, 'AppServer', ProbeAppServer), \\\n"
                "        patch.object(codex_goal, 'auth_secrets', return_value=[]), \\\n"
                "        patch('scripts.ci.codex_session_checkpoint.save_session'):\n"
                "    try:\n"
                "        codex_goal.run_model_goal(Path(os.environ['CODEX_TASK_DIR']),\n"
                "            Path(os.environ['ADAPTATION_PROMPT']))\n"
                "    except RuntimeError:\n"
                "        diagnostic_path = Path(os.environ['CODEX_TASK_DIR']) / 'goal-diagnostics.json'\n"
                "        diagnostic = json.loads(diagnostic_path.read_text())\n"
                "        raw_exit_code = os.environ.get('CODEX_TEST_EXIT_CODE', '37')\n"
                "        exit_code = (True if raw_exit_code == 'bool' else\n"
                "            None if raw_exit_code == 'null' else int(raw_exit_code))\n"
                "        diagnostic['authenticationLoad'] = {'outcome': 'load_error',\n"
                "            'accountType': 'unknown', 'exitCode': exit_code,\n"
                "            'stderr': 'AUTH_LOAD_PRIVATE_SENTINEL'}\n"
                "        diagnostic['unapprovedField'] = 'TOP_LEVEL_PRIVATE_SENTINEL'\n"
                "        diagnostic['controllerError'] = 'CONTROLLER_ERROR_PRIVATE_SENTINEL'\n"
                "        diagnostic['serverErrorTail'] = 'SERVER_ERROR_PRIVATE_SENTINEL'\n"
                "        diagnostic_path.write_text(json.dumps(diagnostic))\n"
                "        raise SystemExit(23)\n"
                "raise SystemExit('authentication preflight unexpectedly succeeded')\n"
            )
            prompt = Path(temporary) / "prompt.md"
            prompt.write_text("unused")
            artifact_directory = Path(temporary) / "uploaded-artifacts"
            env = dict(os.environ)
            env.update({
                "APP_ID": "wechat",
                "CODEX_CONTROLLER_MODULE": "codex_live_auth_failure_probe",
                "CONTROLLER_ROOT": temporary,
                "CODEX_TASK_DIR": str(task),
                "ADAPTATION_PROMPT": str(prompt),
                "CODEX_HOME": str(Path(temporary) / "no-auth-home"),
                "CODEX_TRACE_RECIPIENT": recipient,
                "CODEX_TEST_ARTIFACTS": str(artifact_directory),
                "PYTHONPATH": os.pathsep.join((temporary, str(ROOT))),
            })
            result = subprocess.run(
                [NODE, "--input-type=module", "-e", self._node_script()],
                cwd=ROOT, env=env, text=True, capture_output=True, check=False,
            )

            self.assertNotEqual(0, result.returncode, result.stderr)
            uploaded_diagnostics = list(artifact_directory.rglob("goal-diagnostics.json"))
            self.assertTrue(uploaded_diagnostics,
                            f"live snapshot omitted goal diagnostics: {result.stdout}\n{result.stderr}")
            safe_summary = json.loads(uploaded_diagnostics[-1].read_text())
            self.assertEqual({
                "status": "interrupted",
                "authenticationRefresh": "failed",
                "authenticationFailure": "account_type_mismatch",
                "authenticationLoad": {
                    "outcome": "load_error",
                    "accountType": "unknown",
                    "exitCode": 37,
                },
                "accountRead": {
                    "responseIsDict": True,
                    "accountFieldPresent": True,
                    "accountShape": "dict",
                    "accountType": "unknown",
                    "requiresOpenaiAuth": True,
                },
            }, safe_summary)
            for private_value in (
                "MODEL_MESSAGE_PRIVATE_SENTINEL", "THREAD_PRIVATE_SENTINEL",
                "ACCOUNT_TYPE_PRIVATE_SENTINEL", "EMAIL_PRIVATE_SENTINEL",
                "TOKEN_PRIVATE_SENTINEL", "RESPONSE_PRIVATE_SENTINEL",
                "AUTH_LOAD_PRIVATE_SENTINEL", "TOP_LEVEL_PRIVATE_SENTINEL",
                "CONTROLLER_ERROR_PRIVATE_SENTINEL", "SERVER_ERROR_PRIVATE_SENTINEL",
                "controllerError", "serverErrorTail", "modelMessages", "threadId",
            ):
                self.assertNotIn(private_value, uploaded_diagnostics[-1].read_text())
            self.assertTrue((uploaded_diagnostics[-1].parent / "runner-logs.json.age").is_file())
            self.assertFalse((task / "native-thread.json").exists())

            null_artifacts = Path(temporary) / "uploaded-null-artifacts"
            env["CODEX_TEST_EXIT_CODE"] = "null"
            env["CODEX_TEST_ARTIFACTS"] = str(null_artifacts)
            null_result = subprocess.run(
                [NODE, "--input-type=module", "-e", self._node_script()],
                cwd=ROOT, env=env, text=True, capture_output=True, check=False,
            )
            self.assertNotEqual(0, null_result.returncode, null_result.stderr)
            null_diagnostics = list(null_artifacts.rglob("goal-diagnostics.json"))
            self.assertTrue(null_diagnostics,
                            f"live snapshot omitted null-exit diagnostics: {null_result.stderr}")
            null_summary = json.loads(null_diagnostics[-1].read_text())
            self.assertEqual(None, null_summary["authenticationLoad"]["exitCode"])

            for invalid_exit_code in ("bool", "999999"):
                with self.subTest(exit_code=invalid_exit_code):
                    invalid_artifacts = Path(temporary) / f"uploaded-{invalid_exit_code}-artifacts"
                    env["CODEX_TEST_EXIT_CODE"] = invalid_exit_code
                    env["CODEX_TEST_ARTIFACTS"] = str(invalid_artifacts)
                    invalid_result = subprocess.run(
                        [NODE, "--input-type=module", "-e", self._node_script()],
                        cwd=ROOT, env=env, text=True, capture_output=True, check=False,
                    )
                    self.assertNotEqual(0, invalid_result.returncode, invalid_result.stderr)
                    invalid_diagnostics = list(invalid_artifacts.rglob("goal-diagnostics.json"))
                    self.assertTrue(invalid_diagnostics,
                                    f"live snapshot omitted invalid-exit diagnostics: {invalid_result.stderr}")
                    invalid_summary = json.loads(invalid_diagnostics[-1].read_text())
                    self.assertEqual(None, invalid_summary["authenticationLoad"]["exitCode"])


if __name__ == "__main__":
    unittest.main()
