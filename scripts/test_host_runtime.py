import contextlib
import importlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from scripts.ci.adb_relay import DeviceRelay, validate_request


class HostRuntimeTest(unittest.TestCase):
    def test_fixed_runtime_request_has_host_timeout_and_no_model_arguments(self):
        relay=DeviceRelay.__new__(DeviceRelay)
        relay.policy=SimpleNamespace(app_id='qq')
        relay.directory=Path('/trusted/task')
        relay.workspace=Path('/workspace')
        relay.execute=Mock(return_value=(0,b'ok',b''))
        result=relay.dispatch(['verify-runtime'],Path('/workspace'), 'request')
        self.assertEqual(0,result[0])
        command,cwd,request,timeout=relay.execute.call_args.args
        self.assertEqual('/trusted/task',command[-1])
        self.assertTrue(command[-2].endswith('/host_runtime.py'))
        self.assertEqual(900,timeout)
        self.assertEqual({'cleanup_grace':60},relay.execute.call_args.kwargs)
        with self.assertRaises(ValueError):
            validate_request({'args':['verify-runtime','--duration','1'],'cwd':'/workspace'},Path('/workspace'))

    def test_runtime_request_is_qq_only(self):
        relay=DeviceRelay.__new__(DeviceRelay);relay.policy=SimpleNamespace(app_id='wecom')
        with self.assertRaises(ValueError):
            relay.dispatch(['verify-runtime'],Path('/workspace'),'request')

    def test_host_runtime_uses_independent_verifier(self):
        host=importlib.import_module('scripts.ci.host_runtime')
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'state.json').write_text(json.dumps({'appId':'qq'}))
            with patch.object(host,'verify') as verify, patch('sys.argv',['host_runtime.py',str(root)]), patch.object(host.signal,'signal'),contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(0,host.main())
            verify.assert_called_once_with(root.resolve())

    def test_verification_failure_cannot_pass(self):
        host=importlib.import_module('scripts.ci.host_runtime')
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'state.json').write_text(json.dumps({'appId':'qq'}))
            with patch.object(host,'verify',side_effect=ValueError('rejected')),patch.object(host,'verification_error',return_value='rejected'),patch('sys.argv',['host_runtime.py',str(root)]), patch.object(host.signal,'signal'),contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(1,host.main())

    def test_client_waits_for_fixed_smoke_without_extending_normal_shell(self):
        from scripts.ci.adb_relay_client import request_timeout
        self.assertEqual(1080,request_timeout(['verify-runtime']))
        self.assertEqual(300,request_timeout(['shell','sleep','45']))
        self.assertEqual(300,request_timeout(['verify-runtime','custom']))

    def test_cancelled_verification_is_not_success(self):
        host=importlib.import_module('scripts.ci.host_runtime')
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'state.json').write_text(json.dumps({'appId':'qq'}))
            with patch.object(host,'verify',side_effect=KeyboardInterrupt),patch('sys.argv',['host_runtime.py',str(root)]),patch.object(host.signal,'signal'),contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(130,host.main())

    def test_repeated_termination_does_not_interrupt_cleanup(self):
        host=importlib.import_module('scripts.ci.host_runtime')
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'state.json').write_text(json.dumps({'appId':'qq'}))
            with patch.object(host,'verify'),patch('sys.argv',['host_runtime.py',str(root)]),patch.object(host.signal,'signal') as signals,contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(0,host.main())
                handler=signals.call_args.args[1]
                with self.assertRaises(KeyboardInterrupt):handler(host.signal.SIGTERM,None)
                self.assertIsNone(handler(host.signal.SIGTERM,None))
