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
