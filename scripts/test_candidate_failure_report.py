import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from scripts.ci.write_candidate_failure_report import write_candidate_failure_report


class CandidateFailureReportTest(unittest.TestCase):
    def test_writes_a_fetch_failure_using_the_candidate_report_schema_and_output(self):
        with TemporaryDirectory() as temporary:
            report_path = Path(temporary) / "candidate-report.json"
            output_path = Path(temporary) / "github-output"

            write_candidate_failure_report(
                report_path=report_path,
                output_path=output_path,
                status="FETCH_FAILED",
                reason="The official CDN download failed",
                source_url="https://dldir1v6.qq.com/weixin/android/weixin8080android3220_arm64.apk",
                checked_versions=["8.0.78", "8.0.79"],
                app_id="wechat",
            )

            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertIsNone(report["identity"])
            self.assertEqual("FETCH_FAILED", report["status"])
            self.assertEqual(["8.0.78", "8.0.79"], report["checkedVersions"])
            self.assertEqual(["The official CDN download failed"], report["blockers"])
            self.assertNotIn("suggestedProfile", report)
            self.assertEqual("pipeline_status=FETCH_FAILED\n", output_path.read_text(encoding="utf-8"))

    def test_requires_an_explicit_application_id(self):
        with TemporaryDirectory() as temporary:
            with self.assertRaises(TypeError):
                write_candidate_failure_report(
                    report_path=Path(temporary) / "candidate-report.json",
                    output_path=Path(temporary) / "github-output",
                    status="FETCH_FAILED",
                    reason="The official CDN download failed",
                    source_url="",
                    checked_versions=[],
                )

    def test_rejects_a_non_failure_pipeline_status(self):
        with TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, "failure status"):
                write_candidate_failure_report(
                    report_path=Path(temporary) / "candidate-report.json",
                    output_path=Path(temporary) / "github-output",
                    status="FORMALLY_SUPPORTED",
                    reason="must not use failure writer",
                    source_url="",
                    checked_versions=[],
                    app_id="qq",
                )


if __name__ == "__main__":
    unittest.main()
