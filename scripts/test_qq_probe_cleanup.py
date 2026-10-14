import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from scripts.ci.apps.qq.runtime_probe import observe
from scripts.ci.apps.qq.packet_capture import observer_source_digest


class QqProbeCleanupTest(unittest.TestCase):
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
                if args[:2]==('shell','getprop'):return result('arm64-v8a')
                return result()
            def su(command,**kwargs):
                commands.append(command)
                if command.startswith('sha256sum '):return result(('bad' if bad_hash else binary_hash)+' collector')
                if command.startswith('cat /proc/'):
                    return result('900 (collector) S '+' '.join(['0']*18)+(' 45' if pid=='902' else ' 44'))
                if command.startswith('cat '):return result(pid+' 44' if pid.isdigit() else pid)
                if command.startswith('readlink '):return result('/data/local/tmp/impad-qq-collector' if pid in ('900','902') else '/system/bin/unrelated')
                return result()
            smoke=SimpleNamespace(ROOT=root,EVIDENCE=root,MODULE='fixture.module',MAGISK='/magisk',
                                  APP_PACKAGE='com.tencent.mobileqq',UI_STRATEGY=SimpleNamespace(launcher_component='fixture.activity'),
                                  configure_manager=lambda enabled:None,enable_page_size_backcompat=lambda:None,
                                  snapshot=lambda label:('', ''),nodes=lambda xml:[],adb=device,su=su,
                                  apk_digest=lambda apk:apk_hash)
            with patch('scripts.ci.apps.qq.runtime_probe.manager_state',return_value={'moduleEnabled':False,'scopeVerified':True}),patch('scripts.ci.apps.qq.runtime_probe.subprocess.Popen',side_effect=RuntimeError('transport failed')):
                with self.assertRaisesRegex((RuntimeError,ValueError),'differs|transport failed'):
                    observe(smoke,False)
        self.assertIn('rm -f /data/local/tmp/impad-qq-collector.pid /data/local/tmp/impad-qq-collector',commands)
        kills=[c for c in commands if isinstance(c,str) and c.startswith('kill ')]
        self.assertEqual(kills,['kill 900'] if pid=='900' else [])


if __name__=='__main__':unittest.main()
