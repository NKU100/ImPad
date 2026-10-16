import unittest
from scripts.ci.stock_avd_apps_probe import exit_records, app_outcome


class StockAppsProbeTest(unittest.TestCase):
    def test_subprocess_crash_rejects_visible_login(self):
        text = '''ApplicationExitInfo #0:
 process=com.tencent.mobileqq:MSF reason=2 (SIGNALED) status=7
ApplicationExitInfo #1:
 process=another.app reason=2 (SIGNALED) status=7'''
        records = exit_records(text, 'com.tencent.mobileqq')
        self.assertEqual([dict(process='com.tencent.mobileqq:MSF', reason=2, status=7)], records)
        self.assertEqual('APP_FAILED', app_outcome(records, True))

    def test_clear_data_exit_is_not_crash(self):
        self.assertEqual('LOGIN_UI_VERIFIED', app_outcome([dict(reason=10)], True))
        self.assertEqual('INCONCLUSIVE', app_outcome([], False))
