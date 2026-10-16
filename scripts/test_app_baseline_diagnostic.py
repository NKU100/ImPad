import unittest
from scripts.ci import app_baseline_diagnostic as diagnostic


class BaselineDiagnosticTest(unittest.TestCase):
    def test_native_abort_overrides_visible_login_page(self):
        logs = 'F libc: Fatal signal 6 (SIGABRT), code -1 in tid 6168, pid 5939 (com.tencent.mm)'
        self.assertEqual('APP_CRASHED', diagnostic.outcome(logs, True, 'com.tencent.mm'))

    def test_unrelated_crash_does_not_reject_visible_login_page(self):
        logs = 'F libc: Fatal signal 6 (SIGABRT) in pid 10 (other.app)'
        self.assertEqual('BASELINE_VERIFIED', diagnostic.outcome(logs, True, 'com.tencent.mm'))

    def test_missing_login_is_inconclusive(self):
        self.assertEqual('INCONCLUSIVE', diagnostic.outcome('', False, 'com.tencent.mm'))
