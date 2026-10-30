import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.ci.effective_code_probe_report import validate_report


ROOT = Path(__file__).resolve().parent.parent
FIXTURES_PATH = ROOT / "scripts/probes/effective-code/fixtures.json"


class EffectiveCodeProbeReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixtures = json.loads(FIXTURES_PATH.read_text(encoding="utf-8"))

    def valid_report(self):
        cases = []
        for fixture in self.fixtures["cases"]:
            cases.append({
                "caseName": fixture["name"],
                "observedMarker": fixture["expectedMarker"],
                "expectedMarker": fixture["expectedMarker"],
                "exportedDexSha256": ["a" * 64],
                "loaderTopology": ["PathClassLoader"],
                "originEvidence": {
                    "mechanism": "instrumented definition source",
                    "definitionIdentity": "dex:classes.dex#Lpkg/Target;",
                    "methodBindingIdentity": "Lpkg/Target;->marker()Ljava/lang/String;",
                },
                "status": "SUPPORTED",
            })
        return {
            "schemaVersion": 1,
            "runId": "run-1",
            "device": {"serial": "emulator-5554", "api": 35, "abi": "arm64-v8a"},
            "dependencies": {
                "probeSha256": "b" * 64,
                "dexkitJarSha256": "c" * 64,
                "dexkitNativeSha256": "d" * 64,
            },
            "caseResults": cases,
        }

    def test_complete_self_consistent_report_is_valid(self):
        self.assertEqual(validate_report(self.valid_report(), self.fixtures), [])

    def test_missing_case_is_rejected(self):
        report = self.valid_report()
        report["caseResults"].pop()
        self.assertTrue(any("missing" in error.lower() for error in validate_report(report, self.fixtures)))

    def test_unknown_status_is_rejected(self):
        report = self.valid_report()
        report["caseResults"][0]["status"] = "MAYBE"
        self.assertTrue(validate_report(report, self.fixtures))

    def test_supported_marker_mismatch_is_rejected(self):
        report = self.valid_report()
        report["caseResults"][0]["observedMarker"] = "wrong"
        self.assertTrue(validate_report(report, self.fixtures))

    def test_supported_missing_origin_identity_is_rejected(self):
        report = self.valid_report()
        report["caseResults"][0]["originEvidence"].pop("methodBindingIdentity")
        self.assertTrue(validate_report(report, self.fixtures))

    def test_missing_dependency_hash_is_rejected(self):
        report = self.valid_report()
        del report["dependencies"]["dexkitJarSha256"]
        self.assertTrue(validate_report(report, self.fixtures))

    def test_explicit_unsupported_result_with_reason_is_valid(self):
        report = self.valid_report()
        result = report["caseResults"][0]
        result.update(status="UNSUPPORTED", observedMarker=None, reason="No live origin API")
        result["originEvidence"] = {}
        self.assertEqual(validate_report(report, self.fixtures), [])

    def test_unsupported_wrong_expected_marker_is_rejected(self):
        report = self.valid_report()
        result = report["caseResults"][0]
        result.update(status="UNSUPPORTED", expectedMarker="WRONG", reason="No live origin API")
        errors = validate_report(report, self.fixtures)
        self.assertTrue(any("expectedMarker" in error for error in errors), errors)

    def test_unsupported_wrong_observed_marker_is_rejected(self):
        report = self.valid_report()
        result = report["caseResults"][0]
        result.update(status="UNSUPPORTED", observedMarker="WRONG", reason="No live origin API")
        errors = validate_report(report, self.fixtures)
        self.assertTrue(any("observedMarker" in error for error in errors), errors)

    def test_unsupported_matching_observation_is_valid(self):
        report = self.valid_report()
        result = report["caseResults"][0]
        result.update(status="UNSUPPORTED", reason="No live origin API", originEvidence={})
        self.assertEqual(validate_report(report, self.fixtures), [])

    def test_malformed_nested_values_return_errors(self):
        for report, fixtures in ((None, self.fixtures), (self.valid_report(), None),
                                 ({"caseResults": [None]}, self.fixtures),
                                 (self.valid_report(), {"cases": [None]})):
            with self.subTest(report=report, fixtures=fixtures):
                self.assertTrue(validate_report(report, fixtures))

    def test_cli_broken_json_exits_nonzero(self):
        with tempfile.TemporaryDirectory() as directory:
            report_path = Path(directory) / "report.json"
            report_path.write_text("{broken", encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(ROOT / "scripts/ci/effective_code_probe_report.py"),
                 "--report", str(report_path), "--fixtures", str(FIXTURES_PATH)],
                capture_output=True, text=True, check=False,
            )
        self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
