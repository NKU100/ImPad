import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import time
from unittest.mock import patch, MagicMock
from scripts.ci.adb_relay import DeviceRelay, validate_request


class AdbDeviceTest(unittest.TestCase):
    def test_device_command_uses_fixed_serial_and_socket(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tools = root / 'platform-tools'
            tools.mkdir()
            adb = tools / 'adb'
            adb.write_text('#!/usr/bin/env python3\nimport os,sys,json\nprint(json.dumps({"socket":os.environ["ADB_SERVER_SOCKET"],"args":sys.argv[1:]}))\n')
            adb.chmod(0o755)
            (root / 'state.json').write_text(json.dumps({'appId': 'wechat', 'worktree': str(root), 'runtime': {'socket': str(root / 'adb.sock'), 'serial': 'emulator-5554'}}))
            env = dict(os.environ, ANDROID_HOME=str(root), IMPAD_ADB_BINARY=str(adb))
            relay = DeviceRelay(root, env)
            env['IMPAD_ADB_RELAY'] = str(relay.root)
            try:
                result = subprocess.check_output(['bash', str(Path('scripts/ci/adb-device.sh').resolve()), 'shell', 'id'], env=env, text=True, cwd=root)
            finally:
                relay.close()
            parsed = json.loads(result)
            self.assertEqual('localfilesystem:' + str(root / 'adb.sock'), parsed['socket'])
            self.assertEqual(['-s', 'emulator-5554', 'shell', 'id'], parsed['args'])

    def test_server_commands_and_device_overrides_are_rejected(self):
        env = dict(os.environ, IMPAD_ADB_RELAY='/unused')
        for command in ['kill-server', 'start-server', 'connect', '-s', '-H', '-P', '-L']:
            result = subprocess.run(['bash', 'scripts/ci/adb-device.sh', command], env=env, capture_output=True)
            self.assertEqual(2, result.returncode)

    def test_wrapper_allows_managed_app_launch_command(self):
        env = dict(os.environ, IMPAD_ADB_RELAY='/nonexistent-managed-relay')
        result = subprocess.run(['bash', 'scripts/ci/adb-device.sh', 'app-launch'], env=env, capture_output=True)
        self.assertNotEqual(2, result.returncode)
        self.assertNotIn(b'Use a device command', result.stderr)

    def test_host_rejects_server_overrides_and_outside_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary).resolve()
            commands = [['-s', 'physical', 'shell'], ['kill-server'],
                        ['install', '/etc/passwd'], ['push', '../secret', '/sdcard/secret'],
                        ['pull', '/sdcard/file', '../outside']]
            for args in commands:
                with self.subTest(args=args), self.assertRaises(ValueError):
                    validate_request({'cwd': str(workspace), 'args': args}, workspace)

    def test_relay_preserves_binary_output_and_exit_status(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tools = root / 'platform-tools'
            tools.mkdir()
            adb = tools / 'adb'
            adb.write_text('#!/usr/bin/env python3\nimport sys\nsys.stdout.buffer.write(bytes([0,255,10]))\nsys.stderr.write("device error")\nsys.exit(7)\n')
            adb.chmod(0o755)
            (root / 'state.json').write_text(json.dumps({'appId': 'wechat', 'worktree': str(root), 'runtime': {'socket': str(root / 'adb.sock'), 'serial': 'emulator-5554'}}))
            env = dict(os.environ, ANDROID_HOME=str(root), IMPAD_ADB_BINARY=str(adb))
            relay = DeviceRelay(root, env)
            try:
                result = subprocess.run(['bash', str(Path('scripts/ci/adb-device.sh').resolve()), 'exec-out', 'screencap', '-p'],
                                        env=dict(env, IMPAD_ADB_RELAY=str(relay.root)), capture_output=True, cwd=root)
            finally:
                relay.close()
            self.assertEqual(7, result.returncode)
            self.assertEqual(bytes([0,255,10]), result.stdout)
            self.assertEqual(b'device error', result.stderr)

    def test_cancelled_client_releases_device_queue(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tools = root / 'platform-tools'; tools.mkdir()
            adb = tools / 'adb'
            adb.write_text('#!/usr/bin/env python3\nimport sys,time,pathlib\nif "hang" in sys.argv:\n pathlib.Path("started").touch()\n time.sleep(30)\nelse: print("device")\n')
            adb.chmod(0o755)
            (root / 'state.json').write_text(json.dumps({'appId': 'wechat', 'worktree': str(root), 'runtime': {'socket': str(root / 'adb.sock'), 'serial': 'emulator-5554'}}))
            env = dict(os.environ, ANDROID_HOME=str(root), IMPAD_ADB_BINARY=str(adb))
            relay = DeviceRelay(root, env)
            env['IMPAD_ADB_RELAY'] = str(relay.root)
            wrapper = str(Path('scripts/ci/adb-device.sh').resolve())
            client = subprocess.Popen(['bash', wrapper, 'shell', 'hang'], cwd=root, env=env)
            try:
                deadline = time.monotonic() + 5
                while not (root / 'started').exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue((root / 'started').exists())
                client.terminate(); client.wait(timeout=3)
                started = time.monotonic()
                result = subprocess.check_output(['bash', wrapper, 'get-state'], cwd=root, env=env, timeout=3)
                self.assertEqual(b'device\n', result)
                self.assertLess(time.monotonic() - started, 3)
            finally:
                if client.poll() is None: client.kill(); client.wait()
                relay.close()

    def test_managed_launch_restores_compatibility_before_starting_wechat(self):
        with tempfile.TemporaryDirectory() as temporary:
            relay = DeviceRelay.__new__(DeviceRelay)
            relay.directory = Path(temporary)
            relay.adb = '/sdk/adb'; relay.serial = 'emulator-5554'
            with patch.object(relay, 'execute', return_value=(0,b'',b'')) as execute:
                relay.dispatch(['wechat-launch'], Path('/workspace'), 'request')
            calls = [c.args[0] for c in execute.call_args_list]
            self.assertEqual('wait-for-device', calls[0][-1])
            self.assertIn('bionic.linker.16kb.app_compat.enabled true', calls[1][-1])
            self.assertIn('/debug_ramdisk/magisk', calls[1][-1])
            self.assertEqual(['shell','am','force-stop','com.tencent.mm'], calls[2][3:])
            self.assertEqual(['shell', 'am', 'start', '-W', '-n', 'com.tencent.mm/.ui.LauncherUI'], calls[3][3:])
            launch_steps = relay.directory / 'wechat-launch-steps.jsonl'
            self.assertEqual(4, len(launch_steps.read_text().splitlines()))

    def test_managed_build_and_launch_do_not_accept_arbitrary_arguments(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            for args in [['verify-build','--task','exec'], ['wechat-launch','other.package']]:
                with self.assertRaises(ValueError):
                    validate_request({'args':args,'cwd':str(root)},root)


if __name__ == '__main__':
    unittest.main()
