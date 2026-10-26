import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from scripts.ci.apps.qq.runtime_probe import observe, collect_process_diagnostics, installed_identity, navigate
from scripts.ci.apps.qq.packet_capture import observer_source_digest


class QqProbeCleanupTest(unittest.TestCase):
    def test_installed_identity_hashes_base_and_every_split(self):
        commands = []

        def adb(*args, **kwargs):
            commands.append(args)
            if args[:3] == ('shell', 'pm', 'path'):
                return subprocess.CompletedProcess(args, 0, b'package:/data/app/pkg/base.apk\npackage:/data/app/pkg/split_config.apk\n', b'')
            path = args[-1]
            digest = 'a' * 64 if path.endswith('base.apk') else 'b' * 64
            return subprocess.CompletedProcess(args, 0, f'{digest}  {path}\n'.encode(), b'')

        identity = installed_identity(SimpleNamespace(adb=adb), 'example.pkg')
        self.assertEqual([
            {'path': '/data/app/pkg/split_config.apk', 'sha256': 'b' * 64}
        ], identity['splitApks'])
        self.assertEqual(3, len(commands))

    def test_tcp_isolation_restores_both_families_after_capture_failure(self):
        from scripts.ci.apps.qq.runtime_probe import isolated_tcp
        commands = []
        smoke = SimpleNamespace(
            adb=lambda *args: subprocess.CompletedProcess([], 0, b'1\n', b''),
            su=lambda command, **kwargs: commands.append(command) or subprocess.CompletedProcess([], 1 if ' -S ' in command else 0, b'', b''),
            save=lambda *args: None)
        with self.assertRaisesRegex(RuntimeError, 'capture failed'):
            with isolated_tcp(smoke, 10232):
                raise RuntimeError('capture failed')
        for tool in ('iptables', 'ip6tables'):
            self.assertIn(tool + ' -w -A IMPAD_QQ_CAPTURE -m owner --uid-owner 10232 -j RETURN', commands)
            self.assertIn(tool + ' -w -D OUTPUT ! -o lo -p tcp -j IMPAD_QQ_CAPTURE', commands)
            self.assertIn(tool + ' -w -X IMPAD_QQ_CAPTURE', commands)

    def test_tcp_isolation_refuses_physical_devices(self):
        from scripts.ci.apps.qq.runtime_probe import isolated_tcp
        commands = []
        smoke = SimpleNamespace(
            adb=lambda *args: subprocess.CompletedProcess([], 0, b'0\n', b''),
            su=lambda command, **kwargs: commands.append(command))
        with self.assertRaisesRegex(RuntimeError, 'emulator'):
            with isolated_tcp(smoke, 10232):
                self.fail('Physical device was modified')
        self.assertEqual([], commands)

    def test_log_access_prompt_is_denied_without_granting_access(self):
        from unittest.mock import Mock
        import xml.etree.ElementTree as ET
        xml = '<hierarchy><node package="android" text="Allow QQ to access all device logs?"/><node package="android" text="Don’t allow" clickable="true" enabled="true"/><node package="android" text="Allow one-time access"/></hierarchy>'
        smoke = SimpleNamespace(snapshot=lambda label: ('', xml), nodes=lambda xml: list(ET.fromstring(xml).iter('node')),
                                unique_node=lambda xml, predicate: next((n for n in ET.fromstring(xml).iter('node') if predicate(n)), None),
                                APP_PACKAGE='com.tencent.mobileqq', tap=Mock())
        navigate(smoke)
        self.assertEqual('Don’t allow', smoke.tap.call_args.args[0].get('text'))

    def test_process_diagnostics_preserve_each_module_state(self):
        from unittest.mock import Mock
        smoke = SimpleNamespace(APP_PACKAGE='com.tencent.mobileqq', adb=Mock(), save=Mock())
        for enabled, label in [(False, 'disabled'), (True, 'enabled')]:
            collect_process_diagnostics(smoke, enabled)
            self.assertEqual('qq-process-' + label + '.txt', smoke.save.call_args_list[-2].args[0])
            self.assertEqual('qq-signals-' + label + '.txt', smoke.save.call_args_list[-1].args[0])
        self.assertIn(unittest.mock.call('shell', 'dumpsys', 'activity', 'exit-info',
                                        'com.tencent.mobileqq', timeout=25, check=False), smoke.adb.call_args_list)

    def test_transport_failure_stops_owned_collector_and_removes_binary(self):
        self.check_failure('900', False)

    def test_invalid_pid_does_not_kill_another_process(self):
        self.check_failure('invalid', False)

    def test_remote_hash_failure_removes_staged_binary(self):
        self.check_failure('', True)

    def test_stale_pid_cannot_kill_an_unrelated_process(self):
        self.check_failure('901', True)

    def test_reused_collector_pid_requires_original_starttime(self):
        self.check_failure('902', True)

    def check_failure(self, pid, bad_hash):
        commands=[]
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'qq.apk').write_bytes(b'exact APK')
            tool=root/'qq-probe';tool.mkdir();(tool/'collector').write_bytes(b'collector fixture')
            apk_hash=hashlib.sha256((root/'qq.apk').read_bytes()).hexdigest()
            binary_hash=hashlib.sha256((tool/'collector').read_bytes()).hexdigest()
            (tool/'collector.json').write_text(json.dumps(dict(abi='arm64',observerSha256=observer_source_digest(),binarySha256=binary_hash,ndkVersion='29.0.14206865')))
            def result(text=''):
                return subprocess.CompletedProcess([],0,text.encode(),b'')
            def device(*args,**kwargs):
                commands.append(args)
                if args[:3]==('shell','pm','path'):return result('package:/data/app/qq/base.apk')
                if args[:2]==('shell','sha256sum'):return result(apk_hash+' /data/app/qq/base.apk')
                if args[:3]==('shell','cmd','package'):return result('package:com.tencent.mobileqq uid:10232\n')
                if args[:2]==('shell','getprop'):return result('1' if args[-1]=='ro.kernel.qemu' else 'arm64-v8a')
                return result()
            def su(command,**kwargs):
                commands.append(command)
                if ' -S IMPAD_QQ_CAPTURE' in command:return subprocess.CompletedProcess([],1,b'',b'')
                if command.startswith('sha256sum '):return result(('bad' if bad_hash else binary_hash)+' collector')
                if command.startswith('cat /proc/'):
                    return result('900 (collector) S '+' '.join(['0']*18)+(' 45' if pid=='902' else ' 44'))
                if command.startswith('cat '):return result(pid+' 44' if pid.isdigit() else pid)
                if command.startswith('readlink '):return result('/data/local/tmp/impad-qq-collector' if pid in ('900','902') else '/system/bin/unrelated')
                return result()
            smoke=SimpleNamespace(ROOT=root,EVIDENCE=root,MODULE='fixture.module',MAGISK='/magisk',
                                  APP_PACKAGE='com.tencent.mobileqq',UI_STRATEGY=SimpleNamespace(launcher_component='fixture.activity'),
                                  configure_manager=lambda enabled:None,enable_page_size_backcompat=lambda:None,
                                  snapshot=lambda label:('', ''),nodes=lambda xml:[],adb=device,su=su,save=lambda name,result:None,
                                  apk_digest=lambda apk:apk_hash)
            with patch('scripts.ci.apps.qq.runtime_probe.manager_state',return_value={'moduleEnabled':False,'scopeVerified':True}),patch('scripts.ci.apps.qq.runtime_probe.subprocess.Popen',side_effect=RuntimeError('transport failed')):
                with self.assertRaisesRegex((RuntimeError,ValueError),'differs|transport failed'):
                    observe(smoke,False)
        self.assertIn('rm -f /data/local/tmp/impad-qq-collector.pid /data/local/tmp/impad-qq-collector',commands)
        kills=[c for c in commands if isinstance(c,str) and c.startswith('kill ')]
        self.assertEqual(kills,['kill 900'] if pid=='900' else [])


if __name__=='__main__':unittest.main()
