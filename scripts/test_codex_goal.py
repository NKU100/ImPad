import tempfile
import json
import io
import os
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock
from scripts.ci.codex_goal import run_goal, run_model_goal, redact, write_diagnostics, AppServer


class AuthenticationTest(unittest.TestCase):
    def exercise(self, response=None, error=None, stderr_content=None):
        client = MagicMock()
        client.secrets = ['private-access-token']
        client.messages = []
        client.thread_id = None
        client.input_delivered = False
        client.authentication_refresh = None
        events = []
        def request(*args):
            events.append('refresh')
            if error is not None:
                raise error
            return response
        client.request.side_effect = request
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            if stderr_content is not None:
                (directory / 'app-server.stderr.log').write_text(stderr_content)
            prompt = directory / 'prompt.md'
            prompt.write_text('adapt exact candidate')
            with patch('scripts.ci.codex_goal.AppServer', return_value=client), \
                 patch('scripts.ci.codex_goal.run_goal', side_effect=lambda *args: events.append('goal') or 'verified') as goal, \
                 patch('scripts.ci.codex_goal.auth_secrets', return_value=client.secrets), \
                 patch('scripts.ci.codex_session_checkpoint.save_session'), \
                 patch.dict(os.environ, {'CODEX_HOME': root}):
                caught = None
                try:
                    run_model_goal(directory, prompt)
                except RuntimeError as failure:
                    caught = failure
                report_path = directory / 'goal-diagnostics.json'
                report = json.loads(report_path.read_text()) if report_path.exists() else {}
        return client, events, goal.call_count, caught, report

    def test_refresh_precedes_goal_and_diagnostics_exclude_account_details(self):
        client, events, calls, error, report = self.exercise({
            'account': {'type': 'chatgpt', 'email': 'private@example.com',
                        'access_token': 'private-access-token'}, 'requiresOpenaiAuth': True})
        self.assertEqual(['refresh', 'goal'], events)
        client.request.assert_called_once_with('account/read', {'refreshToken': True})
        self.assertEqual(1, calls)
        self.assertIsNone(error)
        self.assertEqual('succeeded', report['authenticationRefresh'])
        self.assertEqual({
            'responseIsDict': True,
            'accountFieldPresent': True,
            'accountShape': 'dict',
            'accountType': 'chatgpt',
            'requiresOpenaiAuth': True,
        }, report['accountRead'])
        self.assertNotIn('private', json.dumps(report))

    def test_refresh_failure_prevents_goal_and_is_redacted(self):
        _, events, calls, error, report = self.exercise(error=RuntimeError(
            'refresh rejected private-access-token private@example.com account-secret'),
            stderr_content='account response private@example.com account-secret')
        self.assertEqual(['refresh'], events)
        self.assertEqual(0, calls)
        self.assertIsNotNone(error)
        self.assertEqual('failed', report['authenticationRefresh'])
        self.assertEqual('account_read_rpc', report['authenticationFailure'])
        self.assertNotIn('private-access-token', json.dumps(report))
        self.assertNotIn('private@example.com', json.dumps(report))
        self.assertNotIn('account-secret', json.dumps(report))
        self.assertNotIn('private@example.com', str(error))

    def test_missing_managed_account_prevents_goal(self):
        _, events, calls, error, report = self.exercise({'account': None, 'requiresOpenaiAuth': True})
        self.assertEqual(['refresh'], events)
        self.assertEqual(0, calls)
        self.assertIsNotNone(error)
        self.assertEqual('failed', report['authenticationRefresh'])
        self.assertEqual('account_missing', report['authenticationFailure'])
        self.assertEqual({
            'responseIsDict': True,
            'accountFieldPresent': True,
            'accountShape': 'null',
            'accountType': 'unknown',
            'requiresOpenaiAuth': True,
        }, report['accountRead'])

    def test_non_chatgpt_and_unknown_account_types_are_classified_without_raw_values(self):
        for account_type, safe_type in [('apiKey', 'apiKey'), ('amazonBedrock', 'amazonBedrock'),
                                        ('private-account-kind', 'unknown'),
                                        (['private-kind'], 'unknown')]:
            with self.subTest(account_type=safe_type):
                _, events, calls, error, report = self.exercise({
                    'account': {'type': account_type, 'email': 'private@example.com',
                                'token': 'private-access-token'},
                    'requiresOpenaiAuth': 'not-a-bool',
                })
                self.assertEqual(['refresh'], events)
                self.assertEqual(0, calls)
                self.assertIsNotNone(error)
                self.assertIn('account_type_mismatch', str(error))
                self.assertEqual('account_type_mismatch', report['authenticationFailure'])
                self.assertEqual({
                    'responseIsDict': True,
                    'accountFieldPresent': True,
                    'accountShape': 'dict',
                    'accountType': safe_type,
                    'requiresOpenaiAuth': 'unknown',
                }, report['accountRead'])
                serialized = json.dumps(report)
                if isinstance(account_type, str) and account_type not in {'apiKey', 'chatgpt', 'amazonBedrock'}:
                    self.assertNotIn(account_type, serialized)
                self.assertNotIn('private-kind', serialized)
                self.assertNotIn('private@example.com', serialized)
                self.assertNotIn('private-access-token', serialized)

    def test_missing_account_field_and_non_object_response_have_safe_shapes(self):
        for response, failure, expected in [
            ({'requiresOpenaiAuth': False}, 'account_missing', {
                'responseIsDict': True, 'accountFieldPresent': False,
                'accountShape': 'missing', 'accountType': 'unknown',
                'requiresOpenaiAuth': False,
            }),
            ({'account': ['private-content'], 'requiresOpenaiAuth': 0}, 'account_invalid', {
                'responseIsDict': True, 'accountFieldPresent': True,
                'accountShape': 'other', 'accountType': 'unknown',
                'requiresOpenaiAuth': 'unknown',
            }),
            (['unexpected', 'private'], 'account_response_invalid', {
                'responseIsDict': False, 'accountFieldPresent': False,
                'accountShape': 'unavailable', 'accountType': 'unknown',
                'requiresOpenaiAuth': 'unknown',
            }),
        ]:
            with self.subTest(response_is_dict=isinstance(response, dict)):
                _, events, calls, error, report = self.exercise(response)
                self.assertEqual(['refresh'], events)
                self.assertEqual(0, calls)
                self.assertIsNotNone(error)
                self.assertEqual(failure, report['authenticationFailure'])
                self.assertEqual(expected, report['accountRead'])
                self.assertNotIn('unexpected', json.dumps(report))


class FakeClient:
    def __init__(self, status='active'):
        self.calls = []
        self.status = status
        self.turn = 0
    def request(self, method, params):
        self.calls.append((method, params))
        if method == 'command/exec':
            return {'exitCode': 0, 'stdout': 'sandbox ready', 'stderr': ''}
        if method == 'thread/start':
            return {'thread': {'id': 'thread-1'}}
        if method == 'turn/start':
            self.turn += 1
            return {'turn': {'id': str(self.turn)}}
        if method == 'thread/goal/get':
            return {'goal': {'status': self.status, 'tokensUsed': self.turn * 100, 'tokenBudget': 10000}}
        if method == 'thread/read':
            return {'thread': {'turns': []}}
        return {}
    def wait_turn(self, thread, turn):
        return {'status': 'completed'}
    def pause(self, thread):
        self.request('thread/goal/set', {'threadId': thread, 'status': 'paused'})


class GoalTest(unittest.TestCase):
    def exercise(self, statuses, failures=()):
        client = FakeClient()
        states = iter(statuses)
        original = client.request
        def request(method, params):
            if method == 'thread/goal/get':
                client.calls.append((method, params))
                return {'goal': {'status': next(states)}}
            return original(method, params)
        client.request = request
        checkpoints = []
        with tempfile.TemporaryDirectory() as root, patch('scripts.ci.codex_goal.persist_turn', side_effect=lambda *args: checkpoints.append(args[2])), patch('scripts.ci.codex_goal.verify', side_effect=list(failures) or [None]) as verify:
            result = run_goal(client, Path(root), 'adapt')
        return result, client, checkpoints, verify.call_count

    def test_active_turn_does_not_run_independent_acceptance(self):
        result, client, checkpoints, count = self.exercise(['active', 'complete'])
        self.assertEqual('verified', result)
        self.assertEqual(1, count)
        self.assertEqual(3, len(checkpoints))
        self.assertFalse(any('tokenBudget' in params for _, params in client.calls))

    def test_rejected_completion_returns_feedback_to_same_thread(self):
        result, client, checkpoints, count = self.exercise(['complete', 'complete'], [ValueError('missing tablet UI'), None])
        self.assertEqual('verified', result)
        self.assertEqual(2, count)
        turns = [p for m,p in client.calls if m == 'turn/start']
        self.assertEqual(['thread-1', 'thread-1'], [p['threadId'] for p in turns])
        self.assertIn('missing tablet UI', turns[1]['input'][0]['text'])

    def test_blocked_goal_is_saved_without_independent_verification(self):
        result, client, checkpoints, count = self.exercise(['blocked'])
        self.assertEqual('blocked', result)
        self.assertEqual(1, len(checkpoints))
        self.assertEqual(0, count)

    def test_failed_checkpoint_prevents_next_turn(self):
        client = FakeClient()
        with tempfile.TemporaryDirectory() as root, patch('scripts.ci.codex_goal.persist_turn', side_effect=RuntimeError('upload failed')), patch('scripts.ci.codex_goal.verify') as verify:
            with self.assertRaisesRegex(RuntimeError, 'upload failed'):
                run_goal(client, Path(root), 'adapt')
        self.assertEqual(1, client.turn)
        verify.assert_not_called()

    def test_checkpoint_handshake_waits_for_matching_durable_ack(self):
        from scripts.ci.codex_goal import persist_turn
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {'CODEX_CHECKPOINT_HANDSHAKE':'true'}):
            directory = Path(root)
            client = FakeClient();client.thread_id = 'thread-1'
            (directory/'checkpoint-ack.json').write_text(json.dumps({'sequence':1, 'saved':True}))
            finished = threading.Event()
            worker = threading.Thread(target=lambda: (persist_turn(directory, client, 2), finished.set()))
            worker.start()
            try:
                time.sleep(0.1)
                self.assertFalse(finished.is_set())
                (directory/'checkpoint-ack.json').write_text(json.dumps({'sequence':2, 'saved':True}))
                worker.join(2)
                self.assertTrue(finished.is_set())
            finally:
                (directory/'checkpoint-ack.json').write_text(json.dumps({'sequence':2, 'saved':True}))
                worker.join(2)

    def test_rejected_upload_ack_fails_closed(self):
        from scripts.ci.codex_goal import persist_turn
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {'CODEX_CHECKPOINT_HANDSHAKE':'true'}):
            directory = Path(root); client = FakeClient();client.thread_id = 'thread-1'
            (directory/'checkpoint-ack.json').write_text(json.dumps({'sequence':1, 'saved':False, 'error':'upload failed'}))
            with self.assertRaisesRegex(RuntimeError, 'upload failed'):
                persist_turn(directory, client, 1)

    def test_sandbox_failure_stops_before_creating_goal_or_spending_tokens(self):
        client = FakeClient()
        def request(method, params):
            client.calls.append((method, params))
            if method == 'command/exec':
                return {'exitCode': 1, 'stderr': 'bwrap: loopback: Operation not permitted'}
            self.fail('A failed sandbox must stop before creating a thread')
        client.request = request
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(RuntimeError, 'Sandbox preflight failed'):
                run_goal(client, Path(root), 'adapt', 10000)
        self.assertEqual(0, client.turn)

    def test_failed_turn_is_not_verified_or_retried(self):
        client = FakeClient()
        with tempfile.TemporaryDirectory() as root, patch('scripts.ci.codex_goal.persist_turn'), patch('scripts.ci.codex_goal.write_diagnostics'), patch('scripts.ci.codex_goal.verify') as verify, patch.object(client, 'wait_turn', return_value={'status': 'failed', 'error': {'message': 'tool unavailable'}}):
            result = run_goal(client, Path(root), 'adapt')
            verify.assert_not_called()
        self.assertEqual(1, client.turn)
        self.assertEqual('interrupted', result)

    def test_diagnostics_remain_valid_json_after_redaction(self):
        client = FakeClient()
        client.secrets = ['secret-value']
        client.messages = ['Bearer secret-value', 'access_token=secret-value']
        with tempfile.TemporaryDirectory() as root, patch('scripts.ci.codex_goal.auth_secrets', return_value=[]):
            write_diagnostics(Path(root), [{'error': 'secret-value'}], client)
            text = (Path(root) / 'goal-diagnostics.json').read_text()
            report = json.loads(text)
            self.assertNotIn('secret-value', text)
            self.assertEqual('[REDACTED]', report['iterations'][0]['error'])

    def test_nullable_tool_output_does_not_crash_event_reader(self):
        client = AppServer.__new__(AppServer)
        client.trace = MagicMock()
        client.secrets = []
        client.messages = []
        client.tool_output = {}
        client.process = MagicMock()
        client.process.stdout = io.StringIO(json.dumps({'method': 'item/completed', 'params': {'item': {'id': 'tool-1', 'type': 'commandExecution', 'exitCode': 1, 'aggregatedOutput': None}}}) + '\n')
        with patch('scripts.ci.codex_goal.auth_secrets', return_value=[]):
            client.read()
        self.assertEqual(1, len(client.messages))

    def test_known_credentials_and_jwt_are_redacted(self):
        jwt = 'eyJhbGciOiJIUzI1NiJ9.abcdefghijklmno.abcdefghijklmno'
        result = redact('token secret-value ' + jwt, ['secret-value'])
        self.assertNotIn('secret-value', result)
        self.assertNotIn(jwt, result)

class NativeAdbEnvironmentTest(unittest.TestCase):
    def test_actual_model_controller_environment_preserves_native_adb(self):
        from scripts.ci import codex_goal as goal
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'IMPAD_ADB_BINARY': '/usr/bin/adb', 'GH_TOKEN': 'must-not-leak', 'CODEX_HOME': directory}), patch.object(goal, 'AppServer') as server, patch.object(goal, 'run_goal', return_value='verified'), patch('scripts.ci.codex_session_checkpoint.save_session'), patch.object(goal, 'read_json', return_value={}):
            server.return_value.secrets = []
            server.return_value.messages = []
            server.return_value.thread_id = None
            server.return_value.input_delivered = False
            server.return_value.request.return_value = {'account': {'type': 'chatgpt'}}
            root = Path(directory)
            prompt = root / 'prompt.md'; prompt.write_text('Adapt')
            goal.run_model_goal(root, prompt)
            environment = server.call_args.args[1]
            self.assertEqual(environment['IMPAD_ADB_BINARY'], '/usr/bin/adb')
            self.assertNotIn('GH_TOKEN', environment)
