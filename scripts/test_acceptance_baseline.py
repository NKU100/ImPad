from pathlib import Path
import tempfile
import unittest

from scripts.testdata.acceptance_baseline import copy_migration_baseline


class AcceptanceBaselineTest(unittest.TestCase):
    def test_fixture_keeps_frozen_history_and_excludes_growing_acceptance(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'source'
            for app in ('wechat', 'qq', 'wecom'):
                app_dir = source / app
                (app_dir / 'acceptance-evidence/record').mkdir(parents=True)
                (app_dir / 'acceptance.json').write_bytes(b'frozen history')
                (app_dir / 'acceptance-v2.json').write_bytes(b'new registration')
                (app_dir / 'acceptance-evidence/record/report.json').write_bytes(b'new evidence')
            destination = root / 'fixture'
            copy_migration_baseline(source, destination)
            for app in ('wechat', 'qq', 'wecom'):
                self.assertEqual(b'frozen history', (destination / app / 'acceptance.json').read_bytes())
                self.assertFalse((destination / app / 'acceptance-v2.json').exists())
                self.assertFalse((destination / app / 'acceptance-evidence').exists())
                self.assertEqual(b'new registration', (source / app / 'acceptance-v2.json').read_bytes())
