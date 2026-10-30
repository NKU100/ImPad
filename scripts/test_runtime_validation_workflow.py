import os
import json
import ast
import hashlib
import re
import shutil
import textwrap
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile
from unittest.mock import patch

from scripts.ci.runtime_task import _verify_regression_snapshot, register_cli, verify_cli
from scripts.ci.app_policy import get_policy
from scripts.ci.acceptance import AcceptanceSnapshot, read_acceptance_snapshot
from scripts.ci.candidate_selection import (
    dependency_lock_sha256, module_source_hashes, prepare_module,
    route_configuration_sha256, source_tree_sha256, write_local_runtime_assets,
)
from scripts.ci.semantic_protocol import (execution_identity_sha256, plan_execution_sha256,
    snapshot_sha256, validate_check_report)
from scripts.test_semantic_protocol import check_report, catalog as semantic_catalog, plan as semantic_plan, snapshot as semantic_snapshot
from scripts.test_dexkit_gradle_integration import parsed_workflows


def _fixture_python_command(directory, apk_bytes):
    fixture = directory / 'official.apk'
    fixture.write_bytes(apk_bytes)
    wrapper_dir = directory / 'bin'
    wrapper_dir.mkdir(exist_ok=True)
    wrapper = wrapper_dir / 'python3'
    root = Path(__file__).resolve().parents[1]
    wrapper.write_text(textwrap.dedent(f'''\
        #!{sys.executable}
        import os, sys
        sys.path.insert(0, {str(root)!r})
        arguments = sys.argv[1:]
        if len(arguments) >= 2 and arguments[:1] == ['-m'] and arguments[1] in {{
                'scripts.ci.fetch_latest_wechat_apk', 'scripts.ci.candidate_selection'}}:
            import scripts.ci.fetch_latest_wechat_apk as fetcher
            import scripts.ci.candidate_selection as selector
            payload = open(os.environ['TASK_FIXTURE_APK'], 'rb').read()
            url = os.environ['TASK_FIXTURE_URL']
            metadata = {{'content_length': len(payload), 'last_modified': '', 'etag': 'fixture-etag'}}
            class Response:
                status = 200
                headers = {{'Content-Length': str(len(payload)), 'ETag': 'fixture-etag'}}
                def __init__(self): self.offset = 0
                def geturl(self): return url
                def read(self, size=-1):
                    end = len(payload) if size < 0 else min(len(payload), self.offset + size)
                    block = payload[self.offset:end]
                    self.offset = end
                    return block
                def __enter__(self): return self
                def __exit__(self, *_args): return False
            fetcher.urlopen = lambda _request, timeout=30: Response()
            if arguments[1] == 'scripts.ci.fetch_latest_wechat_apk':
                fetcher.remote_metadata = lambda _url, _policy=None: metadata
                sys.argv = [arguments[1], *arguments[2:]]
                raise SystemExit(fetcher.main())
            else:
                selector.remote_metadata = lambda _url, _policy=None: metadata
                sys.argv = [arguments[1], *arguments[2:]]
                selector.main()
        else:
            os.execv({sys.executable!r}, [{sys.executable!r}, *arguments])
    '''))
    wrapper.chmod(0o755)
    return wrapper_dir, fixture


def _workflow_if(expression, context):
    expression = expression.strip()
    if expression.startswith('${{') and expression.endswith('}}'):
        expression = expression[3:-2].strip()
    expression = expression.replace('always()', 'True')
    for name, value in sorted(context.items(), key=lambda item: len(item[0]), reverse=True):
        expression = expression.replace(name, repr(value))
    expression = expression.replace('&&', ' and ').replace('||', ' or ')
    tree = ast.parse(expression, mode='eval').body

    def evaluate(node):
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.BoolOp) and isinstance(node.op, (ast.And, ast.Or)):
            values = [bool(evaluate(value)) for value in node.values]
            return all(values) if isinstance(node.op, ast.And) else any(values)
        if isinstance(node, ast.Compare) and len(node.ops) == len(node.comparators) == 1:
            left, right = evaluate(node.left), evaluate(node.comparators[0])
            if isinstance(node.ops[0], ast.Eq):
                return left == right
            if isinstance(node.ops[0], ast.NotEq):
                return left != right
        raise AssertionError(f'Unsupported workflow condition: {expression!r}')

    return bool(evaluate(tree))


class RuntimeValidationWorkflowTest(unittest.TestCase):
    def test_app_entrypoints_pass_only_supported_inputs_without_retired_controls(self):
        workflows = parsed_workflows()
        shared_inputs = workflows['compatibility-app.yml']['true']['workflow_call']['inputs']
        retired = {'force_recheck', 'run_codex'}
        self.assertTrue(retired.isdisjoint(shared_inputs))
        self.assertNotIn('workflow_dispatch', workflows['compatibility-app.yml']['true'])
        for app in ('wechat', 'qq', 'wecom'):
            with self.subTest(app=app):
                workflow = workflows[f'compatibility-{app}.yml']
                inputs = workflow['true']['workflow_dispatch']['inputs']
                self.assertTrue(retired.isdisjoint(inputs))
                call = workflow['jobs']['compatibility']
                self.assertTrue(set(call['with']).issubset(shared_inputs))
                self.assertTrue(retired.isdisjoint(call['with']))
                self.assertEqual(app, call['with']['app_id'])
                self.assertEqual({'verify', 'adapt'}, set(inputs['operation']['options']))
                self.assertEqual('verify', inputs['operation']['default'])
                self.assertEqual("${{ github.event_name == 'schedule' && 'adapt' || inputs.operation || 'verify' }}",
                                 call['with']['operation'])
                for name in ('module_run_id', 'module_artifact'):
                    self.assertEqual({'type': 'string', 'default': ''},
                                     {key: inputs[name][key] for key in ('type', 'default')})
                    self.assertEqual(f"${{{{ inputs.{name} || '' }}}}", call['with'][name])

    def test_compatibility_concurrency_serializes_only_same_app_adaptations(self):
        workflows = parsed_workflows()
        shared = workflows['compatibility-app.yml']
        self.assertEqual(
            "impad-compatibility-${{ inputs.app_id }}-${{ inputs.operation == 'adapt' && 'adapt' || github.run_id }}",
            shared['concurrency']['group'])
        self.assertFalse(shared['concurrency']['cancel-in-progress'])
        self.assertEqual('max', shared['concurrency']['queue'])
        adapt_lock = shared['jobs']['adapt']['concurrency']
        self.assertEqual('impad-codex-managed-auth', adapt_lock['group'])
        self.assertFalse(adapt_lock['cancel-in-progress'])
        self.assertEqual('max', adapt_lock['queue'])
        for job_name, job in shared['jobs'].items():
            if job_name == 'adapt':
                self.assertEqual(adapt_lock, job['concurrency'])
            else:
                self.assertNotIn('concurrency', job)
        self.assertNotIn('concurrency', workflows['runtime-validation.yml'])
        self.assertIn('workflow_call', workflows['runtime-validation.yml']['true'])
        self.assertNotIn('workflow_dispatch', workflows['runtime-validation.yml']['true'])

    def test_managed_auth_environment_is_scoped_to_adapt_and_persisted_there(self):
        workflows = parsed_workflows()
        shared = workflows['compatibility-app.yml']
        adapt = shared['jobs']['adapt']
        self.assertEqual('codex-managed-auth', adapt['environment'])
        self.assertEqual(['adapt'], [name for name, job in shared['jobs'].items()
                                     if 'environment' in job])

        steps = adapt['steps']
        verify_auth = next(step for step in steps
                           if step.get('name') == 'Verify trusted trigger and authentication configuration')
        self.assertEqual('${{ secrets.CODEX_AUTH_JSON }}', verify_auth['env']['CODEX_AUTH_JSON'])
        self.assertEqual('${{ secrets.CODEX_AUTH_WRITE_TOKEN }}', verify_auth['env']['GH_TOKEN'])
        self.assertIn('environments/codex-managed-auth/secrets/CODEX_AUTH_JSON', verify_auth['run'])
        self.assertIn("jq -r '.name // empty'", verify_auth['run'])
        self.assertIn('== CODEX_AUTH_JSON', verify_auth['run'])
        self.assertIn('environments/codex-managed-auth/secrets/public-key', verify_auth['run'])
        self.assertNotIn('actions/secrets/public-key', verify_auth['run'])

        persist = next(step for step in steps
                       if step.get('name') == 'Persist refreshed authentication even after model failure')
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / 'codex-home'
            home.mkdir()
            auth_file = home / 'auth.json'
            auth_file.write_text('{"fixture":"non-secret-test-payload"}')
            bin_dir = root / 'bin'
            bin_dir.mkdir()
            args_file = root / 'gh-args.json'
            stdin_file = root / 'gh-stdin'
            gh = bin_dir / 'gh'
            gh.write_text(textwrap.dedent(f'''\
                #!{sys.executable}
                import json, pathlib, sys
                pathlib.Path({str(args_file)!r}).write_text(json.dumps(sys.argv[1:]))
                pathlib.Path({str(stdin_file)!r}).write_bytes(sys.stdin.buffer.read())
            '''))
            gh.chmod(0o755)
            result = subprocess.run(
                ['bash', '-e', '-c', persist['run']],
                env=dict(os.environ, PATH=f'{bin_dir}:{os.environ["PATH"]}',
                         CODEX_HOME=str(home), GH_TOKEN='mock-token',
                         GITHUB_REPOSITORY='NKU100/ImPad'),
                capture_output=True, text=True)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(
                ['secret', 'set', 'CODEX_AUTH_JSON', '--repo', 'NKU100/ImPad',
                 '--env', 'codex-managed-auth'],
                json.loads(args_file.read_text()))
            self.assertEqual(auth_file.read_bytes(), stdin_file.read_bytes())

    def test_auth_preflight_rejects_repository_fallback_and_reports_environment_permissions(self):
        shared = parsed_workflows()['compatibility-app.yml']
        verify_auth = next(step for step in shared['jobs']['adapt']['steps']
                           if step.get('name') == 'Verify trusted trigger and authentication configuration')
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bin_dir = root / 'bin'
            bin_dir.mkdir()
            calls_file = root / 'api-calls.jsonl'
            gh = bin_dir / 'gh'
            gh.write_text(textwrap.dedent(f'''\
                #!{sys.executable}
                import json, os, pathlib, sys
                endpoint = sys.argv[2]
                with pathlib.Path({str(calls_file)!r}).open('a') as calls:
                    calls.write(json.dumps(endpoint) + '\\n')
                mode = os.environ['MOCK_GH_MODE']
                if endpoint.endswith('/secrets/CODEX_AUTH_JSON'):
                    if mode == 'missing-secret':
                        print('HTTP 404: Not Found', file=sys.stderr)
                        raise SystemExit(1)
                    print(json.dumps({{'name': 'CODEX_AUTH_JSON'}}))
                elif endpoint.endswith('/secrets/public-key'):
                    if mode == 'forbidden-public-key':
                        print('HTTP 403: Resource not accessible by personal access token', file=sys.stderr)
                        raise SystemExit(1)
                    print('{{}}')
            '''))
            gh.chmod(0o755)
            base_env = dict(
                os.environ, PATH=f'{bin_dir}:{os.environ["PATH"]}',
                GITHUB_REF='refs/heads/main', GITHUB_EVENT_NAME='workflow_dispatch',
                GITHUB_REPOSITORY='NKU100/ImPad',
                CODEX_AUTH_JSON='repository-fallback-test-fixture', GH_TOKEN='mock-token',
                CODEX_CHECKPOINT_RECIPIENT='recipient', CODEX_CHECKPOINT_IDENTITY='identity',
                CODEX_TRACE_RECIPIENT='trace')

            for mode, expected_error, expected_endpoint in (
                ('missing-secret', 'Environment secret CODEX_AUTH_JSON must exist',
                 '/environments/codex-managed-auth/secrets/CODEX_AUTH_JSON'),
                ('forbidden-public-key', 'CODEX_AUTH_WRITE_TOKEN must have Environments read/write',
                 '/environments/codex-managed-auth/secrets/public-key'),
            ):
                with self.subTest(mode=mode):
                    calls_file.unlink(missing_ok=True)
                    result = subprocess.run(
                        ['bash', '-e', '-c', verify_auth['run']],
                        env=dict(base_env, MOCK_GH_MODE=mode), capture_output=True, text=True)
                    self.assertNotEqual(0, result.returncode)
                    self.assertIn(expected_error, result.stderr)
                    self.assertNotIn('repository-fallback-test-fixture', result.stdout + result.stderr)
                    calls = [json.loads(line) for line in calls_file.read_text().splitlines()]
                    self.assertEqual(f'repos/NKU100/ImPad{expected_endpoint}', calls[-1])

    def test_incompatible_analysis_hands_off_only_for_adaptation(self):
        result_job = parsed_workflows()['runtime-validation.yml']['jobs']['result']
        validation = next(step for step in result_job['steps']
                          if step.get('name') == 'Validate pipeline acceptance without publishing support')
        for operation, status, exit_code in (
            ('adapt', 'INCOMPATIBLE', 0), ('verify', 'INCOMPATIBLE', 1), ('adapt', 'ERROR', 1),
        ):
            with self.subTest(operation=operation, status=status), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / 'runtime-result').mkdir()
                result = subprocess.run(['bash', '-e', '-c', validation['run']], cwd=root,
                                        env=dict(os.environ, OPERATION=operation,
                                                 CANDIDATE_STATUS=status,
                                                 GITHUB_STEP_SUMMARY=str(root / 'summary.md')),
                                        capture_output=True, text=True)
                self.assertEqual(exit_code, result.returncode, result.stderr)
                report = json.loads((root / 'runtime-result/runtime-candidate-report.json').read_text())
                self.assertEqual('INCOMPATIBLE', report['outcome'])
                self.assertNotEqual('VERIFIED', report['outcome'])
        smoke_download = next(step for step in result_job['steps']
                              if step.get('uses', '').startswith('actions/download-artifact')
                              and step.get('with', {}).get('path') == 'runtime-result/evidence')
        self.assertEqual("needs.prepare.outputs.candidate_status == 'PREPARED'", smoke_download.get('if'))
        self.assertEqual('${{ inputs.operation }}', result_job['env']['OPERATION'])

    def test_final_adaptation_validation_shell_parses_with_real_controller_for_every_app(self):
        steps = parsed_workflows()['compatibility-app.yml']['jobs']['adapt']['steps']
        validation = next(step for step in steps
                          if step.get('name') == 'Validate adaptation through shared checker and build')
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            wrapper = root / 'python3'
            wrapper.write_text(textwrap.dedent(f'''\
                #!{sys.executable}
                import os, sys
                sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})
                from scripts.ci import codex_adaptation
                parse_args = codex_adaptation.argparse.ArgumentParser.parse_args
                def capture_args(parser, args=None, namespace=None):
                    parsed = parse_args(parser, args, namespace)
                    assert parsed.app_id == os.environ['APP_ID']
                    print('APP_ID:' + parsed.app_id)
                    return parsed
                codex_adaptation.argparse.ArgumentParser.parse_args = capture_args
                codex_adaptation.validate = lambda directory: print('DIRECTORY:' + str(directory))
                sys.argv = [sys.argv[2], *sys.argv[3:]]
                codex_adaptation.main()
            '''))
            wrapper.chmod(0o755)
            for app_id in ('wechat', 'qq', 'wecom'):
                with self.subTest(app_id=app_id):
                    task = root / f'{app_id} task'
                    result = subprocess.run(['bash', '-e', '-c', validation['run']],
                                            env=dict(os.environ, APP_ID=app_id,
                                                     CONTROLLER_ROOT=str(root), CODEX_TASK_DIR=str(task),
                                                     PATH=str(root) + os.pathsep + os.environ['PATH']),
                                            capture_output=True, text=True)
                    self.assertEqual(0, result.returncode, result.stderr)
                    self.assertIn('APP_ID:' + app_id, result.stdout)
                    self.assertIn('DIRECTORY:' + str(task.resolve()), result.stdout)

    def test_adaptation_prepares_app_release_tests_before_offline_gate(self):
        steps = parsed_workflows()['compatibility-app.yml']['jobs']['adapt']['steps']
        preparation = next(step for step in steps
                           if step.get('name') == 'Prepare static validation dependencies')
        self.assertIn(':app:testReleaseUnitTest', preparation['run'])

    def test_module_artifact_pairing_is_resolved_by_the_prepare_shell_steps(self):
        runtime = parsed_workflows()['runtime-validation.yml']
        steps = runtime['jobs']['prepare']['steps']
        contract = next(step for step in steps
                        if step.get('name') == 'Validate operation and candidate artifact contract')
        selection = next(step for step in steps
                         if step.get('name') == 'Resolve exact module artifact selection')
        download = next(step for step in steps if step.get('name') == 'Reuse exact module build')
        self.assertIn('MODULE_ARTIFACT', contract['env'])
        self.assertIn('MODULE_ARTIFACT', contract['run'])
        self.assertEqual("${{ steps.module-artifact.outputs.name }}", download['with']['name'])

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            def run_selection(run_id, artifact, app_id='qq'):
                output = root / f'output-{run_id}-{artifact}'
                result = subprocess.run(
                    ['bash', '-e', '-c', selection['run']],
                    env=dict(os.environ, APP_ID=app_id, ARTIFACT_PREFIX='impad',
                             MODULE_RUN_ID=run_id, MODULE_ARTIFACT=artifact,
                             GITHUB_OUTPUT=str(output)), capture_output=True, text=True)
                return result, output.read_text() if output.exists() else ''

            def output_value(raw):
                match = re.fullmatch(r'name<<([^\n]+)\n(.*?)\n\1\n', raw, re.S)
                if match:
                    return match.group(2)
                match = re.fullmatch(r'name=(?:\n)?', raw)
                if match:
                    return ''
                self.fail(f'Unexpected GITHUB_OUTPUT encoding: {raw!r}')

            artifact_only = {'OPERATION': 'verify', 'CANDIDATE_MODE': 'latest', 'APK_URL': '',
                             'MODULE_RUN_ID': '', 'MODULE_ARTIFACT': 'custom-module',
                             'CANDIDATE_ARTIFACT': '', 'SOURCE_HEAD': ''}
            rejected = subprocess.run(['bash', '-e', '-c', contract['run']],
                                      env=dict(os.environ, **artifact_only),
                                      capture_output=True, text=True)
            self.assertNotEqual(0, rejected.returncode)

            run_only, output = run_selection('12345', '')
            self.assertEqual(0, run_only.returncode, run_only.stderr)
            self.assertEqual('impad-qq-adapted-module', output_value(output))

            custom, output = run_selection('23456', 'chosen-debug-module')
            self.assertEqual(0, custom.returncode, custom.stderr)
            self.assertEqual('chosen-debug-module', output_value(output))

            invalid_run = subprocess.run(
                ['bash', '-e', '-c', contract['run']],
                env=dict(os.environ, **(artifact_only | {
                    'MODULE_RUN_ID': 'run-123', 'MODULE_ARTIFACT': 'chosen-debug-module'})),
                capture_output=True, text=True)
            self.assertNotEqual(0, invalid_run.returncode)

            default_path, output = run_selection('', '')
            self.assertEqual(0, default_path.returncode, default_path.stderr)
            self.assertEqual('', output_value(output))

    def test_reusable_verify_entry_accepts_and_forwards_optional_module_artifact(self):
        workflows = parsed_workflows()
        shared = workflows['compatibility-app.yml']
        reusable_inputs = shared['true']['workflow_call']['inputs']
        verify = shared['jobs']['verify']

        for name in ('module_run_id', 'module_artifact'):
            self.assertEqual('string', reusable_inputs[name]['type'])
            self.assertEqual('', reusable_inputs[name].get('default', ''))
            self.assertFalse(reusable_inputs[name].get('required', False))
        self.assertEqual('verify', verify['with']['operation'])
        self.assertEqual("${{ inputs.module_run_id || '' }}", verify['with']['module_run_id'])
        self.assertEqual("${{ inputs.module_artifact || '' }}", verify['with']['module_artifact'])
        self.assertEqual('latest', verify['with']['candidate_mode'])
        self.assertEqual("inputs.operation == 'verify'", verify['if'])
        self.assertEqual("inputs.operation == 'adapt'", shared['jobs']['prepare']['if'])
        self.assertNotIn('module_run_id', shared['jobs']['rules-analysis']['with'])
        self.assertNotIn('module_artifact', shared['jobs']['rules-analysis']['with'])

    def test_reusable_entry_requires_app_and_carries_exact_url_and_operation(self):
        workflows = parsed_workflows()
        shared = workflows['compatibility-app.yml']
        reusable = shared['true']['workflow_call']['inputs']
        self.assertTrue(reusable['app_id']['required'])
        self.assertNotIn('default', reusable['app_id'])
        self.assertEqual('', reusable['apk_url']['default'])
        runtime = workflows['runtime-validation.yml']
        prepare = runtime['jobs']['prepare']['steps']
        exact = next(step for step in prepare if step.get('name') == 'Prepare requested APK or XAPK URL')
        discovery = next(step for step in prepare if step.get('name') == 'Discover official candidate URL')
        self.assertIn("inputs.apk_url != ''", exact['if'])
        self.assertIn("inputs.apk_url == ''", discovery['if'])
        self.assertIn('--apk-url "$APK_URL"', exact['run'])
        self.assertNotIn('discover_latest_wechat', exact['run'])
        verify = shared['jobs']['verify']
        self.assertEqual('./.github/workflows/runtime-validation.yml', verify['uses'])
        self.assertEqual("${{ inputs.apk_url || '' }}", verify['with']['apk_url'])
        self.assertEqual('verify', verify['with']['operation'])
        self.assertEqual("inputs.operation == 'adapt'", shared['jobs']['prepare']['if'])



    def test_workflow_keeps_pinned_history_separate_from_schema2_candidate_verification(self):
        workflows = parsed_workflows()
        runtime = workflows['runtime-validation.yml']
        prepare_steps = runtime['jobs']['prepare']['steps']
        regression_step = next(step for step in prepare_steps
                               if step.get('name') == 'Run fixed-record static regression')
        self.assertNotIn('--initial-migration', regression_step['run'])
        self.assertNotIn('migration_args', regression_step['run'])
        result_step = next(step for step in runtime['jobs']['result']['steps']
                           if step.get('name') == 'Validate pipeline acceptance without publishing support')
        self.assertNotIn('--initial-migration', result_step['run'])
        self.assertIn('requires a prepared schema2 candidate', result_step['run'])
        self.assertIn('requires a schema2 APK file snapshot report', result_step['run'])
        self.assertIn('selected schema2 complete plan', result_step['run'])
        self.assertIn("compatibility/{os.environ['APP_ID']}/acceptance.json", result_step['run'])
        registration_step = next(step for step in workflows['compatibility-app.yml']['jobs']['runtime-result']['steps']
                                 if step.get('name') == 'Record verified acceptance or rejection')
        self.assertNotIn('--initial-migration', registration_step['run'])
        self.assertIn('--acceptance "$acceptance"', registration_step['run'])
        self.assertIn("expected_acceptance_path=\"compatibility/${APP_ID}/acceptance-v2.json\"",
                      registration_step['run'])
        self.assertIn('read_semantic_acceptance_table', registration_step['run'])
        self.assertIn('assert_evidence_archive_unchanged', registration_step['run'])
        self.assertIn('evidencePaths differ from the ordered v2 archive mapping',
                      registration_step['run'])
        self.assertIn('history_sha256_before', registration_step['run'])
        self.assertIn('git -C tested add -- "${stage_paths[@]}"', registration_step['run'])
        self.assertIn('git -C tested diff --cached --name-only -z', registration_step['run'])
        self.assertIn('--verify-index', registration_step['run'])
        self.assertNotIn('git -C tested add "compatibility/${APP_ID}/acceptance.json"',
                         registration_step['run'])

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shutil.copytree(Path.cwd() / 'compatibility', root / 'compatibility')
            from scripts.ci.semantic_migration import load_migration_bundle
            bundle = load_migration_bundle(root)
            self.assertEqual(4, len(bundle.profiles))
            for app_id in ('wechat', 'qq', 'wecom'):
                historical = root / f'compatibility/{app_id}/acceptance.json'
                self.assertEqual((Path.cwd() / f'compatibility/{app_id}/acceptance.json').read_bytes(),
                                 historical.read_bytes())

    def test_registration_shell_stages_the_registered_table_and_exact_archived_evidence(self):
        """Catch registration staging only acceptance-v2 while leaving sibling evidence uncommitted."""
        step = next(
            item for item in parsed_workflows()['compatibility-app.yml']['jobs']['runtime-result']['steps']
            if item.get('name') == 'Record verified acceptance or rejection')
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            tested = workspace / 'tested'
            tested.mkdir()
            shutil.copytree(Path.cwd() / 'compatibility', tested / 'compatibility')
            init = subprocess.run(['/usr/bin/git', 'init', '-b', 'main', str(tested)],
                                  capture_output=True, text=True)
            self.assertEqual(0, init.returncode, init.stderr)
            subprocess.run(['/usr/bin/git', '-C', str(tested), 'add', 'compatibility'],
                           check=True, capture_output=True, text=True)
            subprocess.run(['/usr/bin/git', '-C', str(tested), '-c', 'user.name=fixture',
                            '-c', 'user.email=fixture@example.invalid', 'commit', '-m', 'baseline'],
                           check=True, capture_output=True, text=True)
            tested_head = subprocess.run(['/usr/bin/git', '-C', str(tested), 'rev-parse', 'HEAD'],
                                         check=True, capture_output=True, text=True).stdout.strip()

            from scripts.ci.acceptance import validate_semantic_acceptance_table
            from scripts.test_semantic_acceptance import SemanticAcceptanceTest
            semantic_fixture = SemanticAcceptanceTest()
            semantic_fixture.setUp()
            self.addCleanup(semantic_fixture.doCleanups)
            record_id = '6dc4c347-4f7d-4bb8-9c05-6e8ef712bf11'
            archive = Path('compatibility/wechat/acceptance-evidence') / record_id
            refs = [
                ('static', archive / 'reports/static.json', b'{"kind":"static"}\n'),
                ('regression', archive / 'reports/regression.json', b'{"kind":"regression"}\n'),
                ('runtime', archive / 'reports/runtime.json', b'{"kind":"runtime"}\n'),
                ('file', archive / 'runtime/logcat.txt', b'captured runtime evidence\n'),
            ]
            record = semantic_fixture.record.copy()
            record['recordId'] = record_id
            record['rawStageEvidence'] = {
                'static': {'path': refs[0][1].as_posix(),
                           'sha256': hashlib.sha256(refs[0][2]).hexdigest()},
                'regression': {'path': refs[1][1].as_posix(),
                               'sha256': hashlib.sha256(refs[1][2]).hexdigest()},
                'runtime': {'path': refs[2][1].as_posix(),
                            'sha256': hashlib.sha256(refs[2][2]).hexdigest()},
                'files': [{'path': refs[3][1].as_posix(),
                           'sha256': hashlib.sha256(refs[3][2]).hexdigest()}],
            }
            mappings = [
                {'sourcePath': f'/fixture/source/{index}', 'path': relative.as_posix(),
                 'sha256': hashlib.sha256(payload).hexdigest()}
                for index, (_, relative, payload) in enumerate(refs)
            ]
            manifest_path = (archive / 'manifest.json').as_posix()
            record['evidenceArchive'] = {
                'path': archive.as_posix(), 'manifestPath': manifest_path,
                'manifestSha256': '', 'files': mappings,
            }
            for _, relative, payload in refs:
                target = tested / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(payload)
            manifest = {'schemaVersion': 1, 'recordId': record_id, 'files': mappings}
            manifest_bytes = (json.dumps(manifest, indent=2) + '\n').encode()
            record['evidenceArchive']['manifestSha256'] = hashlib.sha256(manifest_bytes).hexdigest()
            (tested / manifest_path).write_bytes(manifest_bytes)
            table = {'schemaVersion': 2, 'appId': 'wechat', 'records': [record]}
            validate_semantic_acceptance_table(table, 'wechat', repository=semantic_fixture.root)
            table_fixture = workspace / 'acceptance-v2.json'
            table_fixture.write_text(json.dumps(table, indent=2) + '\n')
            table_path = Path('compatibility/wechat/acceptance-v2.json')
            (tested / table_path).parent.mkdir(parents=True, exist_ok=True)

            evidence_paths = [relative.as_posix() for _, relative, _ in refs] + [manifest_path]
            registration_fixture = workspace / 'registered.json'
            registration_fixture.write_text(json.dumps({
                'schemaVersion': 2, 'status': 'REGISTERED', 'recordId': record_id,
                'acceptancePath': table_path.as_posix(), 'recordCount': 1,
                'evidencePaths': evidence_paths,
                'evidenceManifest': {'path': manifest_path,
                                    'sha256': hashlib.sha256(manifest_bytes).hexdigest()},
            }))
            bin_dir = workspace / 'bin'
            bin_dir.mkdir()
            commands_log = workspace / 'external-commands.jsonl'
            python_wrapper = bin_dir / 'python3'
            python_wrapper.write_text(textwrap.dedent(f'''\
                #!{sys.executable}
                import json, os, pathlib, shutil, sys
                args = sys.argv[1:]
                if args[:2] == ['-m', 'scripts.ci.runtime_task'] and args[2:3] == ['register']:
                    output = pathlib.Path(args[args.index('--output') + 1])
                    shutil.copyfile(os.environ['REGISTERED_TABLE_FIXTURE'],
                                    pathlib.Path('tested/compatibility/wechat/acceptance-v2.json'))
                    output.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(os.environ['REGISTERED_RESULT_FIXTURE'], output)
                    if os.environ.get('STAGE_EXTRA_PATH'):
                        extra = pathlib.Path('tested/unexpected-staged.json')
                        extra.write_text('unexpected\\n')
                        import subprocess
                        subprocess.run(['/usr/bin/git', '-C', 'tested', 'add', str(extra.name)], check=True)
                    if os.environ.get('MUTATE_HISTORICAL'):
                        pathlib.Path('tested/compatibility/wechat/acceptance.json').write_bytes(b'tampered\\n')
                    raise SystemExit(0)
                os.execv({sys.executable!r}, [{sys.executable!r}, *args])
            '''))
            python_wrapper.chmod(0o755)
            git_wrapper = bin_dir / 'git'
            git_wrapper.write_text(textwrap.dedent(f'''\
                #!{sys.executable}
                import json, pathlib, sys
                args = sys.argv[1:]
                if len(args) >= 3 and args[0] == '-C' and args[2] in ('commit', 'push'):
                    with pathlib.Path({str(commands_log)!r}).open('a') as log:
                        log.write(json.dumps(args) + '\\n')
                    raise SystemExit(0)
                import os
                os.execv('/usr/bin/git', ['/usr/bin/git', *args])
            '''))
            git_wrapper.chmod(0o755)
            gh_wrapper = bin_dir / 'gh'
            gh_wrapper.write_text(textwrap.dedent(f'''\
                #!{sys.executable}
                import json, os, pathlib, sys
                args = sys.argv[1:]
                if args[:2] == ['pr', 'view']:
                    print(json.dumps({{'headRefOid': os.environ.get('FIXTURE_PR_HEAD', {tested_head!r}),
                                      'headRefName': 'adapt/wechat',
                                      'baseRefName': os.environ.get('FIXTURE_PR_BASE', 'main'),
                                      'state': os.environ.get('FIXTURE_PR_STATE', 'OPEN')}}))
                elif args[:2] == ['auth', 'setup-git'] or args[:2] == ['pr', 'ready']:
                    pass
                else:
                    raise SystemExit('unexpected gh command: ' + repr(args))
            '''))
            gh_wrapper.chmod(0o755)
            runtime_smoke = workspace / 'runtime-result/evidence/runtime-smoke/evidence/smoke-report.json'
            runtime_smoke.parent.mkdir(parents=True)
            runtime_smoke.write_text('{}\n')
            env = dict(os.environ, PATH=f'{bin_dir}:{os.environ["PATH"]}',
                       APP_ID='wechat', GITHUB_WORKSPACE=str(workspace),
                       GITHUB_OUTPUT=str(workspace / 'github-output'),
                       GITHUB_REPOSITORY='NKU100/ImPad',
                       PYTHONPATH=str(Path(__file__).resolve().parents[1]),
                       GH_TOKEN='fixture-token', ADAPTATION_PR='https://github.com/NKU100/ImPad/pull/1',
                       TESTED_HEAD=tested_head, RUNTIME_CONCLUSION='success',
                       REGISTERED_TABLE_FIXTURE=str(table_fixture),
                       REGISTERED_RESULT_FIXTURE=str(registration_fixture))
            result = subprocess.run(['bash', '-e', '-c', step['run']], cwd=workspace,
                                    env=env, capture_output=True, text=True)
            self.assertEqual(0, result.returncode, result.stderr)

            staged = subprocess.run(['/usr/bin/git', '-C', str(tested), 'diff', '--cached',
                                     '--name-only'], check=True, capture_output=True,
                                    text=True).stdout.splitlines()
            self.assertEqual(sorted([table_path.as_posix(), *evidence_paths]), sorted(staged))
            self.assertEqual((Path.cwd() / 'compatibility/wechat/acceptance.json').read_bytes(),
                             (tested / 'compatibility/wechat/acceptance.json').read_bytes())
            commands = [json.loads(line) for line in commands_log.read_text().splitlines()]
            self.assertEqual(['commit', 'push'], [args[2] for args in commands])
            current_head = subprocess.run(['/usr/bin/git', '-C', str(tested), 'rev-parse', 'HEAD'],
                                          check=True, capture_output=True, text=True).stdout.strip()
            self.assertEqual(tested_head, current_head)

            v1 = tested / 'compatibility/wechat/acceptance.json'
            v1_bytes = v1.read_bytes()
            recorded_commands = len(commands)
            archive_files = [(tested / relative, payload) for _, relative, payload in refs]
            manifest_file = tested / manifest_path

            def clean_index_and_restore_fixture():
                subprocess.run(['/usr/bin/git', '-C', str(tested), 'reset', '--quiet', 'HEAD'],
                               check=True, capture_output=True, text=True)
                (tested / 'unexpected-staged.json').unlink(missing_ok=True)
                v1.write_bytes(v1_bytes)
                for target, payload in archive_files:
                    if target.is_symlink():
                        target.unlink()
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(payload)
                manifest_file.write_bytes(manifest_bytes)
                table_fixture.write_text(json.dumps(table, indent=2) + '\n')
                registration_fixture.write_text(json.dumps({
                    'schemaVersion': 2, 'status': 'REGISTERED', 'recordId': record_id,
                    'acceptancePath': table_path.as_posix(), 'recordCount': 1,
                    'evidencePaths': evidence_paths,
                    'evidenceManifest': {'path': manifest_path,
                                         'sha256': hashlib.sha256(manifest_bytes).hexdigest()},
                }))

            def assert_registration_rejected(label, *, edit_result=None, edit_table=None,
                                             tamper=None, extra_env=None, staged_extra=False):
                clean_index_and_restore_fixture()
                if edit_result:
                    payload = json.loads(registration_fixture.read_text())
                    edit_result(payload)
                    registration_fixture.write_text(json.dumps(payload))
                if edit_table:
                    payload = json.loads(table_fixture.read_text())
                    edit_table(payload)
                    table_fixture.write_text(json.dumps(payload, indent=2) + '\n')
                if tamper:
                    tamper()
                result = subprocess.run(
                    ['bash', '-e', '-c', step['run']], cwd=workspace,
                    env=dict(env, **(extra_env or {})), capture_output=True, text=True)
                self.assertNotEqual(0, result.returncode, f'{label} unexpectedly succeeded')
                self.assertEqual(recorded_commands,
                                 len(commands_log.read_text().splitlines()),
                                 f'{label} reached the commit/push stubs')
                staged_now = subprocess.run(
                    ['/usr/bin/git', '-C', str(tested), 'diff', '--cached', '--name-only'],
                    check=True, capture_output=True, text=True).stdout.splitlines()
                if staged_extra:
                    self.assertEqual(sorted([table_path.as_posix(), *evidence_paths,
                                             'unexpected-staged.json']), sorted(staged_now),
                                     result.stderr)
                else:
                    self.assertEqual([], staged_now, f'{label} staged unexpected paths: {staged_now}')

            assert_registration_rejected(
                'duplicate evidence path',
                edit_result=lambda payload: payload['evidencePaths'].append(payload['evidencePaths'][0]))
            assert_registration_rejected(
                'wrong registered schema',
                edit_result=lambda payload: payload.__setitem__('schemaVersion', 1))
            assert_registration_rejected(
                'wrong registered status',
                edit_result=lambda payload: payload.__setitem__('status', 'PENDING'))
            assert_registration_rejected(
                'wrong v2 acceptance path',
                edit_result=lambda payload: payload.__setitem__(
                    'acceptancePath', 'compatibility/wechat/acceptance.json'))
            assert_registration_rejected(
                'absolute evidence path',
                edit_result=lambda payload: payload['evidencePaths'].__setitem__(
                    0, '/tmp/untrusted.json'))
            assert_registration_rejected(
                'traversal evidence path',
                edit_result=lambda payload: payload['evidencePaths'].__setitem__(
                    0, '../acceptance.json'))
            assert_registration_rejected(
                'wrong record UUID',
                edit_result=lambda payload: payload.__setitem__(
                    'recordId', '12345678-1234-1234-1234-123456789abc'))
            assert_registration_rejected(
                'wrong manifest digest',
                edit_result=lambda payload: payload['evidenceManifest'].__setitem__('sha256', '0' * 64))
            assert_registration_rejected(
                'wrong manifest path',
                edit_result=lambda payload: payload['evidenceManifest'].__setitem__(
                    'path', 'compatibility/wechat/acceptance-evidence/other/manifest.json'))
            assert_registration_rejected(
                'evidence path order changed',
                edit_result=lambda payload: payload['evidencePaths'].reverse())
            assert_registration_rejected(
                'invalid registered table status',
                edit_table=lambda payload: payload['records'][0].__setitem__('status', 'PENDING'))
            assert_registration_rejected(
                'wrong app acceptance table',
                edit_table=lambda payload: payload.__setitem__('appId', 'qq'))
            assert_registration_rejected(
                'archive content hash mismatch',
                tamper=lambda: archive_files[3][0].write_bytes(b'changed raw evidence\n'))
            assert_registration_rejected(
                'archive symlink',
                tamper=lambda: (archive_files[3][0].unlink(),
                                archive_files[3][0].symlink_to(archive_files[0][0])))
            assert_registration_rejected(
                'manifest content mismatch',
                tamper=lambda: manifest_file.write_bytes(manifest_bytes + b' '))
            assert_registration_rejected(
                'unregistered staged file', extra_env={'STAGE_EXTRA_PATH': '1'}, staged_extra=True)
            assert_registration_rejected(
                'closed PR', extra_env={'FIXTURE_PR_STATE': 'CLOSED'})
            assert_registration_rejected(
                'head changed in PR', extra_env={'FIXTURE_PR_BASE': 'main',
                                                  'FIXTURE_PR_STATE': 'OPEN',
                                                  'FIXTURE_PR_HEAD': 'f' * 40})
            assert_registration_rejected(
                'frozen v1 bytes changed during registration',
                extra_env={'MUTATE_HISTORICAL': '1'})

            for staged_change in (False, True):
                clean_index_and_restore_fixture()
                v1.write_bytes(b'changed frozen v1 bytes\n')
                if staged_change:
                    subprocess.run(['/usr/bin/git', '-C', str(tested), 'add',
                                    'compatibility/wechat/acceptance.json'], check=True,
                                   capture_output=True, text=True)
                result = subprocess.run(['bash', '-e', '-c', step['run']], cwd=workspace,
                                        env=env, capture_output=True, text=True)
                self.assertNotEqual(0, result.returncode)
                self.assertEqual(recorded_commands, len(commands_log.read_text().splitlines()))
                clean_index_and_restore_fixture()

    def test_operation_and_exact_candidate_artifact_contracts_are_enforced(self):
        workflows = parsed_workflows()
        compatibility = workflows['compatibility-app.yml']
        operation_step = next(step for step in compatibility['jobs']['prepare']['steps']
                              if step.get('name') == 'Validate requested operation')
        accepted_operation = subprocess.run(
            ['bash', '-e', '-c', operation_step['run']],
            env=dict(os.environ, OPERATION='adapt'), capture_output=True, text=True)
        self.assertEqual(0, accepted_operation.returncode, accepted_operation.stderr)
        verify_operation = subprocess.run(
            ['bash', '-e', '-c', operation_step['run']],
            env=dict(os.environ, OPERATION='verify'), capture_output=True, text=True)
        self.assertNotEqual(0, verify_operation.returncode)
        invalid_operation = subprocess.run(
            ['bash', '-e', '-c', operation_step['run']],
            env=dict(os.environ, OPERATION='publish'), capture_output=True, text=True)
        self.assertNotEqual(0, invalid_operation.returncode)

        runtime = workflows['runtime-validation.yml']
        contract_step = next(step for step in runtime['jobs']['prepare']['steps']
                             if step.get('name') == 'Validate operation and candidate artifact contract')
        base = {'OPERATION': 'verify', 'CANDIDATE_MODE': 'latest', 'APK_URL': '',
                'MODULE_RUN_ID': '', 'CANDIDATE_ARTIFACT': '', 'SOURCE_HEAD': ''}
        for operation in ('verify', 'adapt'):
            result = subprocess.run(['bash', '-e', '-c', contract_step['run']],
                                    env=dict(os.environ, **(base | {'OPERATION': operation})),
                                    capture_output=True, text=True)
            self.assertEqual(0, result.returncode, result.stderr)
        invalid_operation = subprocess.run(
            ['bash', '-e', '-c', contract_step['run']],
            env=dict(os.environ, **(base | {'OPERATION': 'publish'})),
            capture_output=True, text=True)
        self.assertNotEqual(0, invalid_operation.returncode)
        exact = base | {'OPERATION': 'verify', 'CANDIDATE_MODE': 'artifact',
                        'MODULE_RUN_ID': '12345', 'CANDIDATE_ARTIFACT': 'exact-candidate',
                        'SOURCE_HEAD': 'a' * 40}
        result = subprocess.run(['bash', '-e', '-c', contract_step['run'],
                                 ], env=dict(os.environ, **exact), capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)
        for invalid in (
            base | {'CANDIDATE_MODE': 'artifact', 'MODULE_RUN_ID': '12345'},
            exact | {'CANDIDATE_ARTIFACT': 'exact-candidate', 'APK_URL': 'https://official.invalid/apk'},
            exact | {'CANDIDATE_MODE': 'latest'},
            exact | {'SOURCE_HEAD': ''},
            base | {'CANDIDATE_MODE': 'latest', 'CANDIDATE_ARTIFACT': 'unexpected'},
            base | {'CANDIDATE_MODE': 'arbitrary'},
        ):
            with self.subTest(inputs=invalid):
                result = subprocess.run(['bash', '-e', '-c', contract_step['run']],
                                        env=dict(os.environ, **invalid), capture_output=True, text=True)
                self.assertNotEqual(0, result.returncode)

    def test_rules_workflow_passes_official_candidate_and_stage_artifacts_to_verifier(self):
        workflow = Path('.github/workflows/runtime-validation.yml').read_text()
        self.assertNotIn('resolution_mode', workflow)
        self.assertNotIn('LEGACY', workflow)
        self.assertNotIn('targets.json', workflow)
        self.assertIn('discover_latest_wechat --app-id "$APP_ID"', workflow)
        self.assertIn('--apk-url "$APK_URL"', workflow)
        self.assertIn('python3 -m scripts.ci.static_regression', workflow)
        self.assertIn('--app-id "$APP_ID"', workflow)
        self.assertIn('--reports work/runtime-smoke/regression', workflow)
        self.assertNotIn('migration_args=(--initial-migration)', workflow)
        self.assertIn('runtime_task\', \'verify', workflow)
        for argument in ('--candidate', '--module-apk', '--regression', '--smoke',
                         '--evidence-root', '--acceptance', '--output'):
            self.assertIn(argument, workflow)
        self.assertIn('module/app-debug.apk', workflow)
        self.assertIn('module-identity.json', workflow)
        self.assertIn('Validation did not write the formal acceptance table', workflow)

    def test_runtime_artifact_manifest_retains_managed_launch_step_log(self):
        workflows = parsed_workflows()
        smoke_steps = workflows['runtime-validation.yml']['jobs']['smoke']['steps']
        package_step = next(step for step in smoke_steps
                            if step.get('name') == 'Package stable runtime evidence layout')
        upload_step = next(step for step in smoke_steps
                           if 'runtime-smoke' in step.get('with', {}).get('name', ''))
        script = re.search(r"python3 - <<'PYCODE'\n(.*?)\nPYCODE", package_step['run'], re.S)
        self.assertIsNotNone(script)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tooling = root / 'work/runtime-smoke/tooling'
            tooling.mkdir(parents=True)
            (root / 'work/runtime-artifact/runtime-smoke').mkdir(parents=True)
            report = b'{"status":"FAILED","failedStep":"launch-activity"}\n'
            steps = b'{"label":"launch-activity","status":"TIMED_OUT","durationMs":60000}\n'
            (tooling / 'tooling-report.json').write_bytes(report)
            (tooling / 'app-launch-steps.jsonl').write_bytes(steps)
            subprocess.run([sys.executable, '-c', script.group(1)], cwd=root,
                           capture_output=True, text=True, check=True)

            artifact_tooling = root / 'work/runtime-artifact/runtime-smoke/tooling'
            self.assertEqual(report, (artifact_tooling / 'tooling-report.json').read_bytes())
            self.assertEqual(steps, (artifact_tooling / 'app-launch-steps.jsonl').read_bytes())
            upload_paths = upload_step['with']['path'].splitlines()
            self.assertIn('work/runtime-artifact/', [path.strip() for path in upload_paths])


    def test_production_verifier_cli_has_file_inputs_and_json_output_contract(self):
        result = subprocess.run([sys.executable, '-m', 'scripts.ci.runtime_task', 'verify', '--help'],
                                capture_output=True, text=True, check=True)
        for argument in ('--candidate', '--module-apk', '--regression', '--smoke',
                         '--evidence-root', '--acceptance', '--output'):
            self.assertIn(argument, result.stdout)
        self.assertNotIn('--initial-migration', result.stdout)
        self.assertNotIn('--pr', result.stdout)
        self.assertNotIn('--write-acceptance', result.stdout)
        registration = subprocess.run([sys.executable, '-m', 'scripts.ci.runtime_task', 'register', '--help'],
                                      capture_output=True, text=True, check=True)
        for argument in ('--expected-head', '--cancelled', '--candidate', '--module-apk',
                         '--regression', '--smoke', '--acceptance'):
            self.assertIn(argument, registration.stdout)
        self.assertNotIn('--initial-migration', registration.stdout)

    def test_register_cli_rejects_migration_flag_even_for_owner_and_cannot_create_history(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            acceptance_path = root / 'compatibility/wecom/acceptance.json'
            output_path = root / 'registered.json'
            snapshot = AcceptanceSnapshot(acceptance_path, None, None, b'[]', True)
            report = {'outcome': 'VERIFIED', 'candidate': {'appId': 'wecom'}, 'module': {},
                      'static': {'ruleCatalog': {}}, '_acceptanceSnapshotObject': snapshot}
            arguments = [
                '--app-id', 'wecom', '--candidate', str(root / 'candidate.json'),
                '--module-apk', str(root / 'module.apk'), '--regression', str(root / 'regression.json'),
                '--smoke', str(root / 'smoke.json'), '--evidence-root', str(root),
                '--acceptance', str(acceptance_path), '--repository', str(root),
                '--conclusion', 'success', '--expected-head', 'a' * 40,
                '--initial-migration', '--output', str(output_path),
            ]
            with patch.dict(os.environ, {'GITHUB_ACTOR': 'owner', 'GITHUB_REPOSITORY_OWNER': 'owner'}), \
                 patch('scripts.ci.runtime_task.verify_validation_files', return_value=report) as verifier, \
                 patch('scripts.ci.acceptance.make_record', return_value={'recordId': 'new-record'}), \
                 patch('scripts.ci.acceptance.validate_acceptance_snapshot', return_value=[]), \
                 patch('scripts.ci.runtime_task.subprocess.check_output', return_value='a' * 40), \
                 redirect_stderr(StringIO()), self.assertRaises(SystemExit):
                register_cli(arguments)
            verifier.assert_not_called()
            self.assertFalse(acceptance_path.exists())
            self.assertFalse(output_path.exists())

    def test_production_verifier_cli_writes_a_versioned_unavailable_result(self):
        report = {'schemaVersion': 1, 'candidate': {'appId': 'qq'},
                  'outcome': 'UNAVAILABLE', 'failedStage': 'runtime', 'diagnostics': ['missing runtime']}
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'result.json'
            args = ['--app-id', 'qq', '--candidate', 'candidate.json', '--module-apk', 'module.apk',
                    '--regression', 'regression.json', '--smoke', 'smoke.json',
                    '--evidence-root', temporary, '--acceptance', 'acceptance.json',
                    '--repository', '.', '--conclusion', 'failure', '--output', str(output)]
            stdout = StringIO()
            with patch('scripts.ci.runtime_task.verify_validation_files', return_value=report) as verifier, \
                 redirect_stdout(stdout):
                verify_cli(args)
            payload = json.loads(output.read_text())
            self.assertEqual('UNAVAILABLE', payload['outcome'])
            self.assertEqual(1, payload['schemaVersion'])
            self.assertNotIn('recordDraft', payload)
            self.assertNotIn('recordDraft', stdout.getvalue())
            self.assertEqual(Path('acceptance.json'), verifier.call_args.args[-2])
            self.assertFalse(verifier.call_args.args[-1])

    def test_production_verifier_cli_is_pure_even_when_file_validation_is_verified(self):
        report = {'schemaVersion': 1, 'candidate': {'appId': 'qq'}, 'module': {},
                  'outcome': 'VERIFIED', 'failedStage': None, 'stageEvidence': {},
                  'acceptanceSnapshot': {'path': 'acceptance.json', 'sha256': None,
                                         'recordIds': ['existing-record'], 'initialMigration': False}}
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'result.json'
            args = ['--app-id', 'qq', '--candidate', 'candidate.json', '--module-apk', 'module.apk',
                    '--regression', 'regression.json', '--smoke', 'smoke.json',
                    '--evidence-root', temporary, '--acceptance', 'acceptance.json',
                    '--repository', '.', '--conclusion', 'success',
                    '--output', str(output)]
            stdout = StringIO()
            with patch('scripts.ci.runtime_task.verify_validation_files', return_value=report) as verifier, \
                 patch('scripts.ci.acceptance.make_record', side_effect=AssertionError('verify must not register')) as make_record, \
                 redirect_stdout(stdout):
                verify_cli(args)
            payload = json.loads(output.read_text())
            self.assertNotIn('recordDraft', payload)
            self.assertEqual(report['acceptanceSnapshot'], payload['acceptanceSnapshot'])
            self.assertEqual(1, payload['schemaVersion'])
            self.assertEqual({'schemaVersion': 1, 'outcome': 'VERIFIED',
                              'failedStage': None},
                             json.loads(stdout.getvalue()))
            self.assertEqual(Path('acceptance.json'), verifier.call_args.args[-2])
            self.assertFalse(verifier.call_args.args[-1])
            make_record.assert_not_called()

    def test_registration_refuses_untrusted_actor_or_source_head_before_revalidation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / 'registered.json'
            args = ['--app-id', 'qq', '--candidate', 'candidate.json', '--module-apk', 'module.apk',
                    '--regression', 'regression.json', '--smoke', 'smoke.json', '--evidence-root', temporary,
                    '--acceptance', 'compatibility/qq/acceptance.json', '--repository', '.',
                    '--conclusion', 'success', '--expected-head', 'a' * 40,
                    '--output', str(output)]
            with patch.dict(os.environ, {'GITHUB_ACTOR': 'contributor',
                                         'GITHUB_REPOSITORY_OWNER': 'owner'}), \
                 patch('scripts.ci.runtime_task.verify_validation_files') as verifier:
                with self.assertRaisesRegex(ValueError, 'repository owner'):
                    from scripts.ci.runtime_task import register_cli
                    register_cli(args)
                verifier.assert_not_called()

    def test_app_policy_cli_publishes_only_app_specific_inputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / 'github-output'
            output.write_text('')
            subprocess.run([sys.executable, '-m', 'scripts.ci.app_policy', '--app-id', 'qq',
                            '--github-output', str(output)], check=True,
                           env=dict(os.environ, GITHUB_OUTPUT=str(output)))
            values = output.read_text()
            self.assertIn('app_id=qq', values)
            self.assertIn('adaptation_prompt=.github/codex/adapt-qq.md', values)
            self.assertNotIn('resolution_mode', values)
            self.assertNotIn('targets_path', values)

    def test_real_reusable_caller_preserves_candidate_artifact_and_runs_result_verifier(self):
        workflows = parsed_workflows()
        runtime = workflows['runtime-validation.yml']
        caller = workflows['compatibility-app.yml']['jobs']['runtime']
        call_inputs = runtime['true']['workflow_call']['inputs']
        self.assertEqual('./.github/workflows/runtime-validation.yml', caller['uses'])
        self.assertEqual('artifact', caller['with']['candidate_mode'])
        self.assertEqual('${{ github.run_id }}', caller['with']['module_run_id'])
        self.assertIn('runtime-candidate', caller['with']['candidate_artifact'])
        self.assertTrue(call_inputs['candidate_mode']['required'])
        self.assertIn('exact candidate artifact', call_inputs['candidate_mode']['description'])

        prepare_steps = runtime['jobs']['prepare']['steps']
        discover = next(step for step in prepare_steps if step.get('name') == 'Discover official candidate URL')
        prepare_candidate = next(step for step in prepare_steps if step.get('name') == 'Prepare official candidate with the shared checker')
        reuse = next(step for step in prepare_steps if step.get('name') == 'Reuse statically verified candidate')
        conversion = next(step for step in prepare_steps
                          if step.get('name') == 'Recheck exact candidate artifact')
        self.assertEqual('${{ inputs.candidate_artifact }}', reuse['with']['name'])
        caller_context = {'inputs.apk_url': '',
                          'inputs.candidate_artifact': 'impad-qq-runtime-candidate',
                          'inputs.candidate_mode': 'artifact',
                          'inputs.module_run_id': '12345'}
        self.assertFalse(_workflow_if(discover['if'], caller_context))
        self.assertFalse(_workflow_if(prepare_candidate['if'], caller_context))
        self.assertTrue(_workflow_if(reuse['if'], caller_context))
        self.assertTrue(_workflow_if(conversion['if'], caller_context))
        dispatch_context = dict(caller_context, **{'inputs.candidate_artifact': '',
                                                   'inputs.candidate_mode': 'latest',
                                                   'inputs.module_run_id': ''})
        self.assertTrue(_workflow_if(discover['if'], dispatch_context))
        self.assertTrue(_workflow_if(prepare_candidate['if'], dispatch_context))
        self.assertFalse(_workflow_if(conversion['if'], dispatch_context))

        result = runtime['jobs']['result']
        self.assertTrue(_workflow_if(result['if'], caller_context | {'needs.prepare.result': 'success'}))
        self.assertIn('scripts.ci.runtime_task', next(
            step['run'] for step in result['steps']
            if step.get('name') == 'Validate pipeline acceptance without publishing support'))

    def test_reusable_consumer_materializes_the_real_adaptation_artifact_before_prepare(self):
        workflows = parsed_workflows()
        producer = workflows['compatibility-app.yml']['jobs']['adapt']['steps']
        upload = next(step for step in producer if step.get('name') == 'Upload verified runtime candidate')
        self.assertEqual(['runtime-inputs/${{ needs.prepare.outputs.app_id }}-*.apk',
                          'runtime-inputs/candidate-report.json', 'runtime-inputs/apk-inputs/'],
                         [line.strip() for line in upload['with']['path'].splitlines() if line.strip()])
        consumer = workflows['runtime-validation.yml']['jobs']['prepare']['steps']
        downloaded = next(index for index, step in enumerate(consumer)
                          if step.get('name') == 'Reuse statically verified candidate')
        conversions = [index for index, step in enumerate(consumer)
                       if step.get('name') == 'Recheck exact candidate artifact']
        self.assertEqual(1, len(conversions), 'Consumer must materialize the uploaded adaptation candidate')
        conversion = consumer[conversions[0]]
        self.assertGreater(conversions[0], downloaded)
        self.assertIn('candidate_selection', conversion['run'])
        self.assertIn('--adaptation-report', conversion['run'])
        self.assertNotIn('--apk-url', conversion['run'])
        self.assertNotIn('discover_latest_wechat', conversion['run'])
        redroid_prepare = next(index for index, step in enumerate(consumer)
                               if step.get('name') == 'Verify signer and static rules and compile native collector')
        self.assertLess(conversions[0], redroid_prepare)


class AdaptationArtifactHandoffTest(unittest.TestCase):
    def test_uploaded_adaptation_fixture_converts_to_independent_schema2_candidate(self):
        workflows = parsed_workflows()
        producer_steps = workflows['compatibility-app.yml']['jobs']['adapt']['steps']
        export = next(step for step in producer_steps if step.get('name') == 'Export validated runtime inputs')
        upload = next(step for step in producer_steps if step.get('name') == 'Upload verified runtime candidate')
        module_upload = next(step for step in producer_steps if step.get('name') == 'Upload validated module APK')
        consumer_steps = workflows['runtime-validation.yml']['jobs']['prepare']['steps']
        conversion = next(step for step in consumer_steps
                          if step.get('name') == 'Recheck exact candidate artifact')
        self.assertIn('--adaptation-report work/runtime-smoke/candidate-report.json', conversion['run'])
        self.assertNotIn('--apk-url', conversion['run'])
        self.assertIn('${{ inputs.candidate_artifact }}', next(
            step for step in consumer_steps if step.get('name') == 'Reuse statically verified candidate'
        )['with']['name'])

        export_match = re.search(r"python3 - <<'PYEXPORT'\n(.*?)\nPYEXPORT", export['run'], re.S)
        self.assertIsNotNone(export_match)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task = root / 'task'
            checkout = root / 'adaptation-checkout'
            producer_run = root / 'producer-run'
            consumer_root = root / 'consumer' / 'work' / 'runtime-smoke'
            for path in (task, checkout / 'work/analysis', checkout / 'work/apks',
                         checkout / 'app/build/outputs/apk/debug', producer_run, consumer_root / 'module'):
                path.mkdir(parents=True, exist_ok=True)

            candidate_bytes = b'uploaded exact adaptation candidate fixture'
            candidate_sha = hashlib.sha256(candidate_bytes).hexdigest()
            split_bytes = b'uploaded exact adaptation native split'
            split_sha = hashlib.sha256(split_bytes).hexdigest()
            policy = get_policy('qq')
            identity = {'packageName': policy.package_name, 'versionName': '9.3.1',
                        'versionCode': 14378, 'abi': 'arm64-v8a', 'apkSha256': candidate_sha,
                        'signerSha256': policy.signer_sha256}
            catalog = semantic_catalog()

            source_root = Path.cwd()
            sources = module_source_hashes(source_root)
            source_tree = source_tree_sha256(source_root)
            route_config = route_configuration_sha256(sources)
            dependency_lock = dependency_lock_sha256(sources)
            fingerprint = {'schemaVersion': 1, 'moduleSources': sources,
                           'sourceTreeSha256': source_tree,
                           'routeConfigurationSha256': route_config,
                           'dependencyLockSha256': dependency_lock}
            fingerprint_bytes = (json.dumps(fingerprint, separators=(',', ':')) + '\n').encode()
            fingerprint_sha = hashlib.sha256(fingerprint_bytes).hexdigest()
            execution = {'ruleCatalogSha256': catalog['ruleCatalogSha256'],
                         'fingerprintFileSha256': fingerprint_sha,
                         'sourceTreeSha256': source_tree,
                         'routeConfigurationSha256': route_config,
                         'dependencyLockSha256': dependency_lock}
            module_artifact = checkout / 'app/build/outputs/apk/debug/app-debug.apk'
            with zipfile.ZipFile(module_artifact, 'w') as archive:
                archive.writestr('assets/runtime-identity.json', fingerprint_bytes)

            adaptation = {'appId': 'qq', 'status': 'STATIC_VERIFIED_PENDING_RUNTIME',
                          'identity': identity,
                          'sourceUrl': 'https://downv6.qq.com/qqweb/QQ_1/android_apk/9.3.1.apk',
                          'adaptationSummary': {'fixture': True},
                          'apkFiles': [{'path': 'apk-inputs/base.apk', 'role': 'base', 'sha256': candidate_sha},
                                       {'path': 'apk-inputs/native.apk', 'role': 'split', 'sha256': split_sha}]}
            (task / 'state.json').write_text(json.dumps({'worktree': str(checkout)}))
            (task / 'candidate-report.json').write_text(json.dumps(adaptation))
            (checkout / 'work/analysis/candidate-report.json').write_text(json.dumps(adaptation))
            (checkout / f'work/apks/candidate-{candidate_sha}.apk').write_bytes(candidate_bytes)

            (checkout / 'work/analysis/apk-inputs').mkdir()
            (checkout / 'work/analysis/apk-inputs/base.apk').write_bytes(candidate_bytes)
            (checkout / 'work/analysis/apk-inputs/native.apk').write_bytes(split_bytes)
            environment = dict(os.environ, CODEX_TASK_DIR=str(task), APP_ID='qq', PYTHONPATH=str(Path.cwd()))
            subprocess.run([sys.executable, '-c', export_match.group(1)], cwd=producer_run,
                           env=environment, text=True, capture_output=True, check=True)

            uploaded_candidate_files = []
            for path_spec in upload['with']['path'].splitlines():
                pattern = path_spec.strip().replace('${{ needs.prepare.outputs.app_id }}', 'qq')
                matches = sorted(producer_run.glob(pattern))
                self.assertTrue(matches, f'Producer upload input is missing: {pattern}')
                for source in matches:
                    uploaded_candidate_files.append(source.relative_to(producer_run / 'runtime-inputs').as_posix())
                    if source.is_dir():
                        shutil.copytree(source, consumer_root / source.name)
                    else:
                        shutil.copyfile(source, consumer_root / source.name)
            self.assertEqual(['apk-inputs', 'candidate-report.json', f'qq-{candidate_sha}.apk'],
                             sorted(uploaded_candidate_files))

            module_specs = [line.strip() for line in module_upload['with']['path'].splitlines() if line.strip()]
            self.assertEqual(['runtime-inputs/app-debug.apk'], module_specs)
            for spec in module_specs:
                source = producer_run / spec
                self.assertTrue(source.is_file())
                shutil.copyfile(source, consumer_root / 'module' / source.name)

            static_path = consumer_root / 'static-check.json'

            def checker(app_id, apk, report_path, repository=None, abi_splits=()):
                self.assertEqual('qq', app_id)
                self.assertEqual(consumer_root.resolve() / f'qq-{candidate_sha}.apk', Path(apk).resolve())
                self.assertEqual([split_bytes], [path.read_bytes() for path in abi_splits])
                report = check_report()
                report['identity'] = identity
                report['ruleCatalog'] = catalog
                report['inputHashes'] = [{'path': str(Path(apk).resolve()), 'sha256': candidate_sha},
                                         {'path': str(Path(abi_splits[0]).resolve()), 'sha256': split_sha}]
                snap = semantic_snapshot()
                dex_hashes = ['c' * 64, 'e' * 64]
                snap['loaders'][0]['dexSha256'] = dex_hashes
                snap['archives'] = [
                    {'loaderId': 'loader-0', 'containerIndex': 0, 'kind': 'ZIP',
                     'containerSha256': candidate_sha, 'dexSha256': [dex_hashes[0]]},
                    {'loaderId': 'loader-0', 'containerIndex': 1, 'kind': 'ZIP',
                     'containerSha256': split_sha, 'dexSha256': [dex_hashes[1]]},
                ]
                snap['contentSha256'] = snapshot_sha256(snap)
                complete_plan = semantic_plan()
                complete_plan['snapshotSha256'] = snap['contentSha256']
                complete_plan['hooks'][0]['source']['dexSha256'] = dex_hashes[0]
                plan_digest = plan_execution_sha256(complete_plan)
                report['snapshot'] = snap
                report['recognizers'][0]['plans'] = [{'plan': complete_plan,
                    'executionSha256': plan_digest, 'evidence': ['synthetic schema2 fixture']}]
                report['selection'].update(plan=complete_plan, executionSha256=plan_digest,
                    evidenceByRecognizer={'qq_cached_device_enum': ['synthetic schema2 fixture']})
                report['executionIdentity'].update(execution)
                report['executionIdentity']['cacheIdentitySha256'] = execution_identity_sha256(report['executionIdentity'])
                validate_check_report(report, expected_app_id='qq', expected_identity=identity)
                Path(report_path).write_text(json.dumps(report, indent=2) + '\n')
                return report

            cli = conversion['run']
            self.assertIn('--app-id "$APP_ID"', cli)
            argv = ['candidate_selection', '--app-id', 'qq', '--adaptation-report',
                    str(consumer_root / 'candidate-report.json'), '--directory', str(consumer_root)]
            with patch('sys.argv', argv), \
                 patch('scripts.ci.candidate_selection._run_rules_checker', side_effect=checker), \
                 patch('scripts.ci.apps.qq.runtime_probe.manifest_expectation',
                       return_value={'contract': 'QQ_SSO_REQUEST_IDENTITY_V1',
                                     'apkSha256': candidate_sha}), \
                 patch('scripts.ci.candidate_selection.remote_metadata',
                       side_effect=AssertionError('artifact conversion must not rediscover or download')), \
                 patch('scripts.ci.candidate_selection.download_candidate',
                       side_effect=AssertionError('artifact conversion must not rediscover or download')):
                from scripts.ci.candidate_selection import main as candidate_selection_main
                candidate_selection_main()

            candidate_path = consumer_root / 'candidate-report.json'
            candidate = json.loads(candidate_path.read_text())
            self.assertEqual(2, candidate['schemaVersion'])
            self.assertEqual('PREPARED', candidate['status'])
            self.assertEqual(2, len(candidate['apkFiles']))
            self.assertEqual(identity, candidate['identity'])
            self.assertTrue(candidate['expectedPlan']['hooks'])
            self.assertEqual(candidate_sha, hashlib.sha256((consumer_root / f'qq-{candidate_sha}.apk').read_bytes()).hexdigest())
            for row in candidate['apkFiles']:
                path = consumer_root / row['path']
                self.assertTrue(path.is_file())
                self.assertEqual(row['sha256'], hashlib.sha256(path.read_bytes()).hexdigest())
            static = json.loads((consumer_root / candidate['staticReport']).read_text())
            self.assertEqual(static['selection']['plan'], candidate['expectedPlan'])
            self.assertEqual(candidate['staticReportSha256'],
                             hashlib.sha256((consumer_root / candidate['staticReport']).read_bytes()).hexdigest())
            for row in static['inputHashes']:
                self.assertFalse(Path(row['path']).is_absolute())
                path = consumer_root / row['path']
                self.assertTrue(path.is_file())
                self.assertEqual(row['sha256'], hashlib.sha256(path.read_bytes()).hexdigest())

            module_path = consumer_root / 'module/app-debug.apk'
            candidate['module'] = prepare_module(module_path, static, source_root)
            self.assertEqual(2, candidate['module']['schemaVersion'])
            self.assertEqual(candidate['ruleCatalogSha256'], candidate['module']['ruleCatalogSha256'])
            self.assertEqual(candidate['expectedPlan'], static['selection']['plan'])
            self.assertEqual(candidate['staticReportSha256'],
                             hashlib.sha256((consumer_root / candidate['staticReport']).read_bytes()).hexdigest())


class RegressionSnapshotBindingTest(unittest.TestCase):
    def setUp(self):
        self.record = {
            'appId': 'qq', 'recordId': 'record-1',
            'identity': {'apkSha256': 'a' * 64, 'versionCode': 14378},
            'expectedPlan': {'selection': 'UNIQUE', 'ruleId': 'qq_rule',
                            'strategyId': 'qq_strategy', 'targets': {'target': {'descriptor': 'x', 'shape': {}}}},
        }

    def _row(self, status, snapshot_hash):
        return {'appId': 'qq', 'recordId': self.record['recordId'],
                'identity': self.record['identity'], 'expectedPlan': self.record['expectedPlan'],
                'status': status, 'acceptanceSnapshotSha256': snapshot_hash,
                'initialMigration': False}

    def _snapshot(self, records, initial_migration=False):
        raw = b'{"snapshot":1}\n'
        return AcceptanceSnapshot(Path('/tmp/acceptance.json'), raw,
                                  hashlib.sha256(raw).hexdigest(),
                                  json.dumps(records, separators=(',', ':')).encode(),
                                  initial_migration)

    def test_passthrough_requires_exact_fixed_representative_rows_and_snapshot_hash(self):
        with tempfile.TemporaryDirectory() as temporary:
            snapshot = self._snapshot([self.record])
            digest = snapshot.sha256
            with patch('scripts.ci.acceptance.validate_acceptance_snapshot', return_value=[self.record]), \
                 patch('scripts.ci.acceptance.select_representatives', return_value=[self.record]):
                status, binding = _verify_regression_snapshot(
                    [self._row('PASSED', digest)], snapshot, 'qq', False, {}, Path.cwd())
            self.assertEqual('PASSED', status)
            self.assertEqual(digest, binding['sha256'])
            self.assertEqual(['record-1'], binding['recordIds'])

    def test_historical_snapshot_rejects_no_history_missing_extra_duplicate_and_stale_rows(self):
        with tempfile.TemporaryDirectory() as temporary:
            snapshot = self._snapshot([self.record])
            digest = snapshot.sha256
            cases = [
                [dict(self._row('NO_HISTORICAL_SAMPLES', digest), recordId=None)],
                [],
                [self._row('PASSED', digest), dict(self._row('PASSED', digest), recordId='extra')],
                [self._row('PASSED', digest), self._row('PASSED', digest)],
                [self._row('PASSED', '0' * 64)],
            ]
            for rows in cases:
                with self.subTest(rows=rows), \
                     patch('scripts.ci.acceptance.validate_acceptance_snapshot', return_value=[self.record]), \
                     patch('scripts.ci.acceptance.select_representatives', return_value=[self.record]):
                    with self.assertRaises(ValueError):
                        _verify_regression_snapshot(rows, snapshot, 'qq', False, {}, Path.cwd())

    def test_empty_history_requires_explicit_initial_migration_and_one_no_history_row(self):
        with tempfile.TemporaryDirectory() as temporary:
            snapshot = AcceptanceSnapshot(Path(temporary) / 'missing-acceptance.json', None, None,
                                          b'[]', True)
            non_migration_snapshot = AcceptanceSnapshot(snapshot.path, None, None, b'[]', False)
            row = {'appId': 'qq', 'status': 'NO_HISTORICAL_SAMPLES',
                   'acceptanceSnapshotSha256': None, 'initialMigration': True,
                   'note': 'Initial migration has no historical regression samples'}
            with patch('scripts.ci.acceptance.validate_acceptance_snapshot', return_value=[]), \
                 patch('scripts.ci.acceptance.select_representatives', return_value=[]):
                status, binding = _verify_regression_snapshot([row], snapshot, 'qq', True, {}, Path.cwd())
            self.assertEqual('NO_HISTORICAL_SAMPLES', status)
            self.assertIsNone(binding['sha256'])
            self.assertTrue(binding['initialMigration'])
            with patch('scripts.ci.acceptance.validate_acceptance_snapshot', return_value=[]), \
                 patch('scripts.ci.acceptance.select_representatives', return_value=[]):
                with self.assertRaisesRegex(ValueError, 'explicit initial migration'):
                    _verify_regression_snapshot([row], non_migration_snapshot, 'qq', False, {}, Path.cwd())
            with patch('scripts.ci.acceptance.validate_acceptance_snapshot', return_value=[]), \
                 patch('scripts.ci.acceptance.select_representatives', return_value=[]):
                with self.assertRaises(ValueError):
                    _verify_regression_snapshot([row, row], snapshot, 'qq', True, {}, Path.cwd())


class AcceptanceSnapshotAbaTest(unittest.TestCase):
    def _record(self, apk_sha, descriptor):
        identity = {'packageName': 'com.tencent.mobileqq', 'versionName': '9.3.1',
                    'versionCode': 14378, 'abi': 'arm64-v8a', 'apkSha256': apk_sha,
                    'signerSha256': get_policy('qq').signer_sha256}
        plan = {'selection': 'UNIQUE', 'ruleId': 'qq_device_type_001',
                'strategyId': 'qq_tablet_001', 'targets': {'device_type': {
                    'descriptor': descriptor, 'shape': {'parameters': [],
                    'returnType': 'void', 'isStatic': True}}}}
        route = {'appId': 'qq', 'candidates': ['qq_device_type_001']}
        route_sha = hashlib.sha256(json.dumps(route, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        return {
            'recordId': 'stable-record-id', 'schemaVersion': 1, 'appId': 'qq', 'status': 'VERIFIED',
            'identity': identity,
            'source': {'type': 'official', 'url': 'https://downv6.qq.com/qqweb/QQ_1/android_apk/9.3.1.apk',
                       'apkFiles': [{'path': f'apk-inputs/{apk_sha}.apk', 'role': 'base', 'sha256': apk_sha}]},
            'expectedPlan': plan, 'ruleOrder': 1, 'firstForRule': True,
            'firstRecordId': 'stable-record-id', 'ruleCatalogSha256': 'd' * 64,
            'route': route,
            'module': {'sha256': 'b' * 64, 'sourceTreeSha256': 'c' * 64,
                       'ruleCatalogSha256': 'd' * 64, 'routeSha256': route_sha,
                       'routeConfigurationSha256': '7' * 64, 'dependencyLockSha256': 'f' * 64,
                       'fingerprintFileSha256': '8' * 64,
                       'fingerprint': {'schemaVersion': 1, 'sourceTreeSha256': 'c' * 64,
                                       'routeConfigurationSha256': '7' * 64,
                                       'dependencyLockSha256': 'f' * 64},
                       'route': route},
            'stageEvidence': {name: {'path': f'{name}.json', 'sha256': str(index) * 64}
                              for index, name in enumerate(('static', 'regression', 'runtime'), start=1)},
        }

    def test_aba_snapshot_with_same_record_id_cannot_supply_different_valid_plan(self):
        record_a = self._record('a' * 64, 'Lx/Y;->a()V')
        record_b = self._record('e' * 64, 'Lx/Z;->b()V')
        catalog = {'schemaVersion': 1, 'appId': 'qq', 'ruleCatalogSha256': 'd' * 64,
                   'definitions': [{'ruleId': 'qq_device_type_001', 'strategyId': 'qq_tablet_001',
                                    'requiredTargetIds': ['device_type']}],
                   'bindings': [{'ruleId': 'qq_device_type_001', 'min': 14378, 'max': None}],
                   'strategyIds': ['qq_tablet_001']}
        raw_a = (json.dumps({'schemaVersion': 1, 'appId': 'qq', 'records': [record_a]},
                            separators=(',', ':')) + '\n').encode()
        raw_b = (json.dumps({'schemaVersion': 1, 'appId': 'qq', 'records': [record_b]},
                            separators=(',', ':')) + '\n').encode()
        snapshot_digest = hashlib.sha256(raw_a).hexdigest()
        regression_row = {'appId': 'qq', 'recordId': record_b['recordId'],
                          'identity': record_b['identity'], 'expectedPlan': record_b['expectedPlan'],
                          'status': 'PASSED', 'acceptanceSnapshotSha256': snapshot_digest,
                          'initialMigration': False}

        with tempfile.TemporaryDirectory() as temporary:
            snapshot = Path(temporary) / 'acceptance.json'
            snapshot.write_bytes(raw_a)
            original_read_bytes = Path.read_bytes
            calls = 0

            def aba_read_bytes(path, *args, **kwargs):
                nonlocal calls
                if Path(path).resolve() == snapshot.resolve():
                    calls += 1
                    captured = original_read_bytes(path, *args, **kwargs)
                    snapshot.write_bytes(raw_b)
                    snapshot.write_bytes(raw_a)
                    return captured
                return original_read_bytes(path, *args, **kwargs)

            with patch.object(Path, 'read_bytes', aba_read_bytes):
                verified_snapshot = read_acceptance_snapshot(snapshot, 'qq', False, catalog, Path.cwd())
                with self.assertRaisesRegex(ValueError, 'representative|fixed acceptance'):
                    _verify_regression_snapshot([regression_row], verified_snapshot, 'qq', False,
                                                catalog, Path.cwd())
            self.assertEqual(1, calls)
            self.assertEqual(raw_a, snapshot.read_bytes())


if __name__ == '__main__':
    unittest.main()
