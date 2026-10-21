import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.ci.adb_relay import DeviceRelay, normalize_adb_server_socket


class RuntimeToolingProbeTest(unittest.TestCase):
    def test_endpoint_normalization_preserves_tcp_and_filesystem_schemes(self):
        self.assertEqual('tcp:localhost:5039', normalize_adb_server_socket('tcp:localhost:5039'))
        self.assertEqual('localfilesystem:/tmp/impad.sock',
                         normalize_adb_server_socket('localfilesystem:/tmp/impad.sock'))
        self.assertEqual('localfilesystem:/tmp/legacy.sock',
                         normalize_adb_server_socket('/tmp/legacy.sock'))

    def test_relay_compatibly_normalizes_legacy_path_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task = root / 'task'
            task.mkdir()
            workspace = root / 'checkout'
            workspace.mkdir()
            (task / 'state.json').write_text(json.dumps({
                'appId': 'wecom', 'worktree': str(workspace),
                'runtime': {'socket': str(root / 'legacy.sock'), 'serial': '127.0.0.1:16555'},
            }))
            relay = DeviceRelay(task, {'IMPAD_ADB_BINARY': '/bin/true'})
            try:
                self.assertEqual('localfilesystem:' + str(root / 'legacy.sock'),
                                 relay.env['ADB_SERVER_SOCKET'])
            finally:
                relay.close()

    def test_managed_app_launch_persists_safe_timed_step_records(self):
        relay = DeviceRelay.__new__(DeviceRelay)
        relay.policy = __import__('scripts.ci.app_policy', fromlist=['get_policy']).get_policy('wecom')
        relay.adb = '/sdk/adb'
        relay.serial = '127.0.0.1:16555'
        relay.env = {}
        with tempfile.TemporaryDirectory() as directory:
            relay.directory = Path(directory)
            calls = []

            def execute(command, cwd, request, timeout):
                calls.append((command, timeout))
                return 0, f'out-{len(calls)}'.encode(), f'err-{len(calls)}'.encode()

            with patch.object(relay, 'execute', side_effect=execute):
                self.assertEqual(0, relay.dispatch(['app-launch'], Path('/workspace'), 'request')[0])

            rows = [json.loads(line) for line in (relay.directory / 'app-launch-steps.jsonl').read_text().splitlines()]
            self.assertEqual(['wait-for-device', 'page-size-compatibility', 'force-stop', 'launch-activity'],
                             [row['label'] for row in rows])
            self.assertEqual([60, 60, 60, 60], [timeout for _, timeout in calls])
            for index, row in enumerate(rows, 1):
                self.assertEqual('PASSED', row['status'])
                self.assertEqual(0, row['exitCode'])
                self.assertIsInstance(row['startedAt'], str)
                self.assertIsInstance(row['finishedAt'], str)
                self.assertGreaterEqual(row['durationMs'], 0)
                self.assertEqual(f'out-{index}', row['stdout'])
                self.assertEqual(f'err-{index}', row['stderr'])
                self.assertNotIn('command', row)

    def test_failed_managed_step_is_identified_and_keeps_outputs(self):
        relay = DeviceRelay.__new__(DeviceRelay)
        relay.policy = __import__('scripts.ci.app_policy', fromlist=['get_policy']).get_policy('wecom')
        relay.adb = '/sdk/adb'
        relay.serial = '127.0.0.1:16555'
        relay.env = {}
        with tempfile.TemporaryDirectory() as directory:
            relay.directory = Path(directory)
            calls = []

            def execute(command, cwd, request, timeout):
                calls.append(command)
                if len(calls) == 2:
                    return 124, b'partial stdout', b'Managed command exceeded 60 seconds'
                return 0, b'', b''

            with patch.object(relay, 'execute', side_effect=execute):
                code, _, _ = relay.dispatch(['app-launch'], Path('/workspace'), 'request')

            rows = [json.loads(line) for line in (relay.directory / 'app-launch-steps.jsonl').read_text().splitlines()]
            self.assertEqual(124, code)
            self.assertEqual(2, len(rows))
            self.assertEqual('page-size-compatibility', rows[-1]['label'])
            self.assertEqual('TIMED_OUT', rows[-1]['status'])
            self.assertEqual('partial stdout', rows[-1]['stdout'])
            self.assertIn('exceeded 60 seconds', rows[-1]['stderr'])

    def _fake_adb(self, root, serial):
        log = root / 'fake-adb.jsonl'
        executable = root / 'adb'
        executable.write_text(
            '#!/usr/bin/env python3\n'
            'import json, os, pathlib, sys, time\n'
            'args=sys.argv[1:]\n'
            'joined=" ".join(args)\n'
            'fail_marker=os.environ.get("FAKE_ADB_FAIL_MARKER", "setprop bionic.linker.16kb.app_compat.enabled true")\n'
            'path=pathlib.Path(os.environ["FAKE_ADB_LOG"])\n'
            'connected=path.with_suffix(path.suffix+".connected")\n'
            'with path.open("a") as stream: stream.write(json.dumps({"endpoint":os.environ.get("ADB_SERVER_SOCKET"),"args":args})+"\\n")\n'
            f'if args == ["devices"]:\n'
            f' print("List of devices attached")\n'
            f' endpoint=os.environ.get("ADB_SERVER_SOCKET", "")\n'
            f' if endpoint.startswith("tcp:") or (endpoint.startswith("localfilesystem:") and not endpoint.endswith("/private-adb.sock")) or connected.exists(): print("{serial}\\tdevice")\n'
            ' sys.exit(0)\n'
            f'if args == ["connect", "{serial}"]:\n'
            ' connected.write_text("connected")\n'
            ' sys.exit(0)\n'
            'if fail_marker and fail_marker in joined:\n'
            ' print("bounded stdout"); print("bounded stderr", file=sys.stderr); sys.exit(124)\n'
            'if "exec-out" in args and "screencap" in args:\n'
            ' sys.stdout.buffer.write(b"\\x89PNG\\r\\n\\x1a\\nfixture")\n'
            'if args[-3:] == ["shell", "sleep", "30"]: time.sleep(30)\n'
            'if args and args[-1] == "get-state": print("device")\n'
            'sys.exit(0)\n'
        )
        executable.chmod(0o755)
        return executable, log

    def test_probe_preserves_inherited_tcp_and_copies_timeout_diagnostics_before_cleanup(self):
        from scripts.ci import runtime_tooling_probe
        from scripts.ci.redroid_environment_probe import SERIAL

        for endpoint in ('tcp:localhost:5039', 'localfilesystem:/tmp/impad-inherited.sock'):
            with self.subTest(endpoint=endpoint), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary) / 'probe'
                root.mkdir()
                adb, command_log = self._fake_adb(Path(temporary), SERIAL)
                environment = {
                    'IMPAD_ADB_BINARY': str(adb),
                    'FAKE_ADB_LOG': str(command_log),
                    'ADB_SERVER_SOCKET': endpoint,
                }
                with patch.dict(os.environ, environment, clear=False), \
                        self.assertRaisesRegex(RuntimeError, 'page-size-compatibility'):
                    runtime_tooling_probe.probe(root, 'wecom')

                task = root / 'task'
                state = json.loads((task / 'state.json').read_text())
                self.assertEqual(endpoint, state['runtime']['socket'])
                records = [json.loads(line) for line in command_log.read_text().splitlines()]
                self.assertTrue(any(row['endpoint'] == endpoint for row in records))
                self.assertFalse(any('kill-server' in row['args'] for row in records))
                self.assertFalse(any('start-server' in row['args'] for row in records))
                copied = root / 'app-launch-steps.jsonl'
                self.assertTrue(copied.is_file())
                self.assertEqual('page-size-compatibility', json.loads(copied.read_text().splitlines()[-1])['label'])
                report = json.loads((root / 'tooling-report.json').read_text())
                self.assertEqual('FAILED', report['status'])
                self.assertEqual('page-size-compatibility', report['failedStep'])

    def test_probe_owns_only_its_private_filesystem_server_when_no_socket_is_inherited(self):
        from scripts.ci import runtime_tooling_probe
        from scripts.ci.redroid_environment_probe import SERIAL

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'probe'
            root.mkdir()
            adb, command_log = self._fake_adb(Path(temporary), SERIAL)
            with patch.dict(os.environ, {'IMPAD_ADB_BINARY': str(adb), 'FAKE_ADB_LOG': str(command_log)}, clear=False):
                os.environ.pop('ADB_SERVER_SOCKET', None)
                with self.assertRaisesRegex(RuntimeError, 'page-size-compatibility'):
                    runtime_tooling_probe.probe(root, 'wecom')

            state = json.loads((root / 'task/state.json').read_text())
            endpoint = state['runtime']['socket']
            self.assertTrue(endpoint.startswith('localfilesystem:'))
            records = [json.loads(line) for line in command_log.read_text().splitlines()]
            self.assertTrue(any(row['args'][:2] == ['-L', endpoint] and row['args'][2:] == ['start-server']
                                for row in records))
            self.assertTrue(any(row['args'][:2] == ['-L', endpoint] and row['args'][2:] == ['kill-server']
                                for row in records))
            self.assertFalse(any(row['args'] == ['kill-server'] for row in records))

    def test_probe_preserves_later_app_launch_timeout_step_after_client_temp_cleanup(self):
        from scripts.ci import runtime_tooling_probe
        from scripts.ci.redroid_environment_probe import SERIAL

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'probe'
            root.mkdir()
            adb, command_log = self._fake_adb(Path(temporary), SERIAL)
            environment = {
                'IMPAD_ADB_BINARY': str(adb),
                'FAKE_ADB_LOG': str(command_log),
                'FAKE_ADB_FAIL_MARKER': 'am start -W -n',
                'ADB_SERVER_SOCKET': 'tcp:localhost:5039',
            }
            with patch.dict(os.environ, environment, clear=False), \
                    self.assertRaisesRegex(RuntimeError, 'launch-activity'):
                runtime_tooling_probe.probe(root, 'wecom')

            rows = [json.loads(line) for line in (root / 'app-launch-steps.jsonl').read_text().splitlines()]
            self.assertEqual(['wait-for-device', 'page-size-compatibility', 'force-stop', 'launch-activity'],
                             [row['label'] for row in rows])
            self.assertEqual('TIMED_OUT', rows[-1]['status'])
            self.assertEqual('bounded stdout', rows[-1]['stdout'].strip())
            self.assertEqual('bounded stderr', rows[-1]['stderr'].strip())
            self.assertNotIn('command', rows[-1])
            report = json.loads((root / 'tooling-report.json').read_text())
            self.assertEqual('launch-activity', report['failedStep'])
            self.assertEqual('TIMED_OUT', report['failedStatus'])
            self.assertTrue(report['retainedStepLog'])
            self.assertFalse(any(row['args'] == ['kill-server'] for row in
                                 map(json.loads, command_log.read_text().splitlines())))

    def test_private_endpoint_full_managed_probe_records_all_four_steps(self):
        from scripts.ci import runtime_tooling_probe
        from scripts.ci.redroid_environment_probe import SERIAL

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'probe'
            root.mkdir()
            adb, command_log = self._fake_adb(Path(temporary), SERIAL)
            environment = {'IMPAD_ADB_BINARY': str(adb), 'FAKE_ADB_LOG': str(command_log),
                           'FAKE_ADB_FAIL_MARKER': 'no-matching-command'}
            with patch.dict(os.environ, environment, clear=False):
                os.environ.pop('ADB_SERVER_SOCKET', None)
                self.assertTrue(runtime_tooling_probe.probe(root, 'wecom'))

            rows = [json.loads(line) for line in (root / 'app-launch-steps.jsonl').read_text().splitlines()]
            self.assertEqual(4, len(rows))
            self.assertTrue(all(row['status'] == 'PASSED' for row in rows))
            self.assertEqual([60, 60, 60, 60], [row.get('timeoutSeconds', 60) for row in rows])
            report = json.loads((root / 'tooling-report.json').read_text())
            self.assertEqual('PASSED', report['status'])
            self.assertTrue(report['serverOwned'])
            records = list(map(json.loads, command_log.read_text().splitlines()))
            endpoint = json.loads((root / 'task/state.json').read_text())['runtime']['socket']
            self.assertTrue(any(row['args'][:2] == ['-L', endpoint] and row['args'][-1] == 'kill-server'
                                for row in records))
            self.assertEqual('PASSED', report['serverShutdown'])
            start_index = next(index for index, row in enumerate(records)
                               if row['args'][:2] == ['-L', endpoint] and row['args'][-1] == 'start-server')
            connect_index = next(index for index, row in enumerate(records)
                                 if row['args'] == ['connect', SERIAL])
            devices_index = next(index for index, row in enumerate(records)
                                 if row['args'] == ['devices'])
            self.assertLess(start_index, connect_index)
            self.assertLess(connect_index, devices_index)


if __name__ == '__main__':
    unittest.main()
