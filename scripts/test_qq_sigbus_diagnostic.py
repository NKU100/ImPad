import unittest
from scripts.ci.qq_sigbus_diagnostic import msf_exits, outcome


class SigbusDiagnosticTest(unittest.TestCase):
    def test_only_exact_msf_signal_seven_counts(self):
        text = '''ApplicationExitInfo #0:
 timestamp=2026-10-15 01:42:42.862 pid=15970 realUid=10230
 process=com.tencent.mobileqq:MSF reason=2 (SIGNALED) subreason=0 status=7
ApplicationExitInfo #1:
 timestamp=2026-10-15 01:42:43.000 pid=16000 realUid=10230
 process=com.tencent.mobileqq reason=2 (SIGNALED) subreason=0 status=7
ApplicationExitInfo #2:
 timestamp=2026-10-15 01:42:44.000 pid=16001 realUid=10230
 process=com.tencent.mobileqq:MSF reason=10 (USER REQUESTED) status=0
'''
        records = msf_exits(text)
        self.assertEqual([15970, 16001], [r['pid'] for r in records])
        self.assertEqual('SIGBUS_REPRODUCED', outcome(records, True))
        self.assertEqual('INCONCLUSIVE', outcome([], False))
        self.assertEqual('NOT_REPRODUCED', outcome([], True))
        self.assertEqual('OTHER_PROCESS_FAILURE', outcome([dict(signal=0, reason=3)], True))


if __name__ == '__main__':
    unittest.main()

class DiagnosticLayerTest(unittest.TestCase):
    def test_layer_contract(self):
        from scripts.ci.qq_sigbus_diagnostic import layer_contract
        self.assertEqual((False, False, False), layer_contract('stock'))
        self.assertEqual((True, False, False), layer_contract('magisk'))
        self.assertEqual((True, True, False), layer_contract('zygisk'))
        self.assertEqual((True, False, False), layer_contract('zygisk-next'))
        self.assertEqual((True, False, True), layer_contract('zygisk-next-lsposed'))
        self.assertEqual((True, False, True), layer_contract('neozygisk-lsposed'))
        self.assertEqual((True, True, True), layer_contract('lsposed'))
        with self.assertRaises(ValueError):
            layer_contract('module')


class ZygiskNextStateTest(unittest.TestCase):
    def test_rejects_settings_that_did_not_survive_reboot(self):
        from unittest.mock import patch
        from subprocess import CompletedProcess
        from scripts.ci import qq_sigbus_diagnostic as diagnostic
        for status in (b'memory_type:0\nlinker:1\n', b'memory_type:1\nlinker:0\n', b'linker:0\n'):
            with self.subTest(status=status), patch.dict('os.environ', {'QQ_NEXT_LINKER': 'system'}), \
                    patch.object(diagnostic.smoke, 'save'), \
                    patch.object(diagnostic.smoke, 'su', return_value=CompletedProcess([], 0, status)):
                with self.assertRaisesRegex(RuntimeError, 'Zygisk Next settings mismatch'):
                    diagnostic.verify_next_settings()

    def test_accepts_verified_system_linker_without_anonymous_memory(self):
        from unittest.mock import patch
        from subprocess import CompletedProcess
        from scripts.ci import qq_sigbus_diagnostic as diagnostic
        with patch.dict('os.environ', {'QQ_NEXT_LINKER': 'system'}), \
                patch.object(diagnostic.smoke, 'save'), \
                patch.object(diagnostic.smoke, 'su', return_value=CompletedProcess([], 0, b'memory_type:0\nlinker:0\n')):
            diagnostic.verify_next_settings()

    def test_requires_builtin_zygisk_off(self):
        from unittest.mock import patch
        from subprocess import CompletedProcess
        from scripts.ci import qq_sigbus_diagnostic as diagnostic
        with patch.object(diagnostic.smoke, 'bootstrap_magisk'), patch.object(diagnostic.smoke, 'adb'), \
                patch.object(diagnostic.smoke, 'save'), patch.object(diagnostic.smoke, 'reboot'), \
                patch.object(diagnostic.smoke, 'apk_digest', return_value='ce50b13fecb9e4456db69ef17b715f7cfbc094004dd9f30f7d6348dfab619a96'), \
                patch.object(diagnostic.smoke, 'su') as su:
            su.side_effect = [CompletedProcess([], 0, b''), CompletedProcess([], 0, b''), CompletedProcess([], 0, b''),
                              CompletedProcess([], 0, b'uid=0'), CompletedProcess([], 0, b'value=1')]
            with self.assertRaisesRegex(RuntimeError, 'Built-in Zygisk is not disabled'):
                diagnostic.prepare_layer('zygisk-next')

    def test_requires_next_library_in_zygote(self):
        from unittest.mock import patch
        from subprocess import CompletedProcess
        from scripts.ci import qq_sigbus_diagnostic as diagnostic
        with patch.object(diagnostic.smoke, 'bootstrap_magisk'), patch.object(diagnostic.smoke, 'adb'), \
                patch.object(diagnostic.smoke, 'save'), patch.object(diagnostic.smoke, 'reboot'), \
                patch.object(diagnostic.time, 'sleep'), \
                patch.object(diagnostic.smoke, 'apk_digest', return_value='ce50b13fecb9e4456db69ef17b715f7cfbc094004dd9f30f7d6348dfab619a96'), \
                patch.object(diagnostic.smoke, 'su') as su:
            su.side_effect = [CompletedProcess([], 0, b''), CompletedProcess([], 0, b''), CompletedProcess([], 0, b''),
                              CompletedProcess([], 0, b'uid=0'), CompletedProcess([], 0, b'value=0')] + [CompletedProcess([], 0, b'')] * 45
            with self.assertRaisesRegex(RuntimeError, 'library absent from zygote64'):
                diagnostic.prepare_layer('zygisk-next')


class NextRuntimePreparationTest(unittest.TestCase):
    def test_full_runtime_uses_next_framework(self):
        from unittest.mock import patch
        from subprocess import CompletedProcess
        from scripts.ci import runtime_smoke as runtime
        with patch.dict('os.environ', {'QQ_DIAGNOSTIC_LAYER': 'zygisk-next-lsposed'}), \
                patch.object(runtime, 'APP_ID', 'qq'), patch.object(runtime, 'adb', return_value=CompletedProcess([], 0, b'package:/data/app/module/base.apk')), \
                patch.object(runtime, 'save'), patch.object(runtime, 'enable_page_size_backcompat'), \
                patch.object(runtime, 'mobile_input', return_value='<hierarchy/>'), \
                patch.object(runtime, 'reboot'), patch.object(runtime, 'configure_manager'), \
                patch.object(runtime, 'su', return_value=CompletedProcess([], 0, b'701\nmemory_type:0\nlinker:1\n')), \
                patch('scripts.ci.qq_sigbus_diagnostic.prepare_layer') as prepare:
            runtime.prepare_environment({})
            prepare.assert_called_once_with('zygisk-next-lsposed', runtime=runtime)
