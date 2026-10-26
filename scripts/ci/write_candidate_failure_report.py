import argparse
import json
import os
from pathlib import Path
import tempfile

from scripts.ci.app_policy import get_policy
from scripts.ci.download_url import validate_download_url


FAILURE_STATUSES = {
    "FETCH_FAILED",
    "IDENTITY_REJECTED",
    "BASELINE_INVALID",
    "STATIC_REGRESSION_FAILED",
    "RUNTIME_REJECTED",
}


def write_candidate_failure_report(
    report_path: Path,
    output_path: Path,
    status: str,
    reason: str,
    source_url: str,
    checked_versions: list[str],
    app_id: str,
) -> None:
    if status not in FAILURE_STATUSES:
        raise ValueError(f"{status} is not a failure status")
    if source_url:
        validate_download_url(source_url)
    if not reason.strip():
        raise ValueError("failure reason must not be empty")

    report = {
        "appId": get_policy(app_id).app_id,
        "identity": None,
        "sourceUrl": source_url or None,
        "baselineVersion": None,
        "checkedVersions": checked_versions,
        "status": status,
        "hooks": [],
        "blockers": [reason],
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=report_path.parent,
            prefix=f".{report_path.name}-",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            json.dump(report, temporary, ensure_ascii=False, indent=2, sort_keys=True)
            temporary.write("\n")
        os.replace(temporary_path, report_path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("a", encoding="utf-8") as output:
        output.write(f"pipeline_status={status}\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app-id", required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--github-output", type=Path, required=True)
    parser.add_argument("--status", choices=sorted(FAILURE_STATUSES), required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--source-url", default="")
    parser.add_argument("--checked-versions", default="")
    arguments = parser.parse_args()

    write_candidate_failure_report(
        report_path=arguments.report,
        output_path=arguments.github_output,
        status=arguments.status,
        reason=arguments.reason,
        source_url=arguments.source_url,
        checked_versions=[version for version in arguments.checked_versions.split(",") if version],
        app_id=arguments.app_id,
    )
    print(f"Pipeline status: {arguments.status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
