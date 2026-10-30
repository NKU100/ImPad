import json
import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).parent / 'probes/effective-code/run_semantic_inputs.py'


class SemanticInputRunnerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(SCRIPT.parent))
        try:
            spec = importlib.util.spec_from_file_location('semantic_probe_runner', SCRIPT)
            cls.runner = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cls.runner)
        finally:
            sys.path.pop(0)

    def test_signer_parser_accepts_both_sdk_labels_and_ignores_source_stamp(self):
        digest = 'a' * 64
        for label in ['Signer #1', 'V2 Signer:', 'V3.1 Signer:']:
            self.assertEqual(digest, self.runner.verified_signer(
                label + ' certificate SHA-256 digest: ' + digest + '\n' +
                'Source Stamp Signer: certificate SHA-256 digest: ' + 'b' * 64))

    def test_manifest_attributes_do_not_confuse_codename_with_package_name(self):
        actual = self.runner.parse_badging("package: name='com.tencent.mm' versionCode='3040' versionName='8.0.69' compileSdkVersionCodename='16'")
        self.assertEqual({'packageName': 'com.tencent.mm', 'versionCode': 3040, 'versionName': '8.0.69'}, actual)

    def test_distinct_signers_are_rejected_instead_of_trusting_only_first(self):
        with self.assertRaises(ValueError):
            self.runner.verified_signer('Signer #1 certificate SHA-256 digest: ' + 'a' * 64 +
                '\nSigner #2 certificate SHA-256 digest: ' + 'b' * 64)

    def run_invalid(self, serial, manifest, message):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            source = root / 'cases.json'
            source.write_text(json.dumps(manifest))
            output = root / 'output'
            result = subprocess.run([sys.executable, str(SCRIPT), '--serial', serial, '--cases', str(source),
                                     '--output', str(output)], capture_output=True, text=True)
            self.assertNotEqual(0, result.returncode)
            self.assertIn(message, result.stderr)
            self.assertFalse(output.exists())

    def test_physical_device_is_rejected_before_any_case_or_sdk_work(self):
        self.run_invalid('fixture-phone-a', {}, 'emulator-N')

    def test_case_name_cannot_escape_remote_temporary_directory(self):
        self.run_invalid('emulator-5586', {'schemaVersion': 1, 'cases': [{'name': '../outside'}]}, 'Invalid case name')

    def test_duplicate_case_names_cannot_overwrite_previous_evidence(self):
        self.run_invalid('emulator-5586', {'schemaVersion': 1, 'cases': [{'name': 'same'}, {'name': 'same'}]}, 'Duplicate case name')

    def test_empty_matrix_is_not_a_successful_probe(self):
        self.run_invalid('emulator-5586', {'schemaVersion': 1, 'cases': []}, 'at least one case')


if __name__ == '__main__':
    unittest.main()
