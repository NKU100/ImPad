import importlib.util
import tempfile
import unittest
from pathlib import Path

PATH = Path(__file__).parent / 'probes/effective-code/run_probe.py'


class EffectiveCodeProbeRunnerTest(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('effective_code_runner', PATH)
        self.runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.runner)

    def test_phone_or_default_serial_is_rejected(self):
        for serial in ('', 'fixture-phone-a', 'fixture-phone-b', 'emulator-abc', 'emulator-5586;id'):
            with self.subTest(serial=serial), self.assertRaises(ValueError):
                self.runner.validate_serial(serial)
        self.runner.validate_serial('emulator-5586')

    def test_existing_output_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(FileExistsError):
                self.runner.create_output(Path(d))

    def test_dependency_hash_mismatch_stops_before_device_work(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / 'library'
            target.write_bytes(b'incorrect')
            with self.assertRaises(ValueError):
                self.runner.verify_hash(target, '0' * 64)

    def test_multidex_archive_has_distinct_ordered_entries(self):
        import zipfile
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / 'fixture.jar'
            self.runner.write_archive(target, [b'first dex', b'second dex'])
            with zipfile.ZipFile(target) as z:
                self.assertEqual(z.namelist(), ['classes.dex', 'classes2.dex'])
                self.assertEqual(z.read('classes2.dex'), b'second dex')


if __name__ == '__main__':
    unittest.main()
