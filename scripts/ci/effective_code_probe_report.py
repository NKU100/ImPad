#!/usr/bin/env python3
"""Validate structure and internal consistency of effective-code probe reports."""

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
_STATUSES = {"SUPPORTED", "UNSUPPORTED", "ERROR"}
_REQUIRED_DEPENDENCIES = ("probeSha256", "dexkitJarSha256", "dexkitNativeSha256")


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def validate_report(report: dict, fixtures: dict) -> list[str]:
    """Return validation errors; an empty list is not proof of runtime capability."""
    errors: list[str] = []
    if not isinstance(report, dict):
        return ["report must be an object"]
    if not isinstance(fixtures, dict):
        return ["fixtures must be an object"]

    if report.get("schemaVersion") != 1 or isinstance(report.get("schemaVersion"), bool):
        errors.append("schemaVersion must be 1")
    if not _nonempty_string(report.get("runId")):
        errors.append("runId must be a nonempty string")

    device = report.get("device")
    if not isinstance(device, dict):
        errors.append("device must be an object")
    else:
        if not (isinstance(device.get("serial"), str) and device["serial"].startswith("emulator-")
                and len(device["serial"]) > len("emulator-")):
            errors.append("device.serial must be a nonempty emulator-* serial")
        if not isinstance(device.get("api"), int) or isinstance(device.get("api"), bool) or device["api"] <= 0:
            errors.append("device.api must be a positive integer")
        if not _nonempty_string(device.get("abi")):
            errors.append("device.abi must be a nonempty string")

    dependencies = report.get("dependencies")
    if not isinstance(dependencies, dict):
        errors.append("dependencies must be an object")
    else:
        for name in _REQUIRED_DEPENDENCIES:
            if not _is_sha256(dependencies.get(name)):
                errors.append(f"dependencies.{name} must be a 64-character SHA-256 hex digest")

    fixture_cases = fixtures.get("cases")
    expected: dict[str, Any] = {}
    if not isinstance(fixture_cases, list):
        errors.append("fixtures.cases must be a list")
        fixture_cases = []
    for index, case in enumerate(fixture_cases):
        if not isinstance(case, dict):
            errors.append(f"fixtures.cases[{index}] must be an object")
            continue
        name = case.get("name")
        if not _nonempty_string(name):
            errors.append(f"fixtures.cases[{index}].name must be a nonempty string")
            continue
        if name in expected:
            errors.append(f"duplicate fixture case {name!r}")
        else:
            expected[name] = case.get("expectedMarker")
        if not _nonempty_string(case.get("expectedMarker")):
            errors.append(f"fixture {name!r} expectedMarker must be a nonempty string")

    results = report.get("caseResults")
    if not isinstance(results, list):
        errors.append("caseResults must be a list")
        results = []
    seen: set[str] = set()
    for index, result in enumerate(results):
        prefix = f"caseResults[{index}]"
        if not isinstance(result, dict):
            errors.append(f"{prefix} must be an object")
            continue
        name = result.get("caseName")
        if not _nonempty_string(name):
            errors.append(f"{prefix}.caseName must be a nonempty string")
            continue
        if name not in expected:
            errors.append(f"unknown case {name!r}")
        if name in seen:
            errors.append(f"duplicate case result {name!r}")
        seen.add(name)

        status = result.get("status")
        if not isinstance(status, str) or status not in _STATUSES:
            errors.append(f"{prefix}.status must be SUPPORTED, UNSUPPORTED, or ERROR")
            continue
        if status == "ERROR":
            errors.append(f"{prefix} has ERROR status")
        reason = result.get("reason")
        if status in {"UNSUPPORTED", "ERROR"} and not _nonempty_string(reason):
            errors.append(f"{prefix}.reason is required for {status}")

        marker = result.get("observedMarker")
        expected_marker = result.get("expectedMarker")
        if marker is not None and not isinstance(marker, str):
            errors.append(f"{prefix}.observedMarker must be a string or null")
        if not isinstance(expected_marker, str):
            errors.append(f"{prefix}.expectedMarker must be a string")
        if name in expected:
            if expected_marker != expected[name]:
                errors.append(f"{prefix}.expectedMarker does not match fixture")
            if marker is not None and marker != expected[name]:
                errors.append(f"{prefix}.observedMarker does not match fixture")

        exports = result.get("exportedDexSha256")
        if not isinstance(exports, list):
            errors.append(f"{prefix}.exportedDexSha256 must be a list")
        else:
            for digest_index, digest in enumerate(exports):
                if not _is_sha256(digest):
                    errors.append(f"{prefix}.exportedDexSha256[{digest_index}] must be a SHA-256 hex digest")

        topology = result.get("loaderTopology")
        if not isinstance(topology, list):
            errors.append(f"{prefix}.loaderTopology must be a list")
        elif any(not _nonempty_string(item) for item in topology):
            errors.append(f"{prefix}.loaderTopology entries must be nonempty strings")

        origin = result.get("originEvidence")
        if not isinstance(origin, dict):
            errors.append(f"{prefix}.originEvidence must be an object")
            origin = {}
        if status == "SUPPORTED":
            if marker is None:
                errors.append(f"{prefix} SUPPORTED result requires observedMarker")
            if not exports:
                errors.append(f"{prefix} SUPPORTED result requires exported DEX hashes")
            if not isinstance(topology, list) or not topology:
                errors.append(f"{prefix} SUPPORTED result requires loaderTopology")
            for field in ("mechanism", "definitionIdentity", "methodBindingIdentity"):
                if not _nonempty_string(origin.get(field)):
                    errors.append(f"{prefix} SUPPORTED result requires originEvidence.{field}")

    for name in expected:
        if name not in seen:
            errors.append(f"missing case result {name!r}")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--fixtures", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        report = json.loads(args.report.read_text(encoding="utf-8"))
        fixtures = json.loads(args.fixtures.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        print(f"error: unable to read JSON input: {exc}", file=sys.stderr)
        return 2

    errors = validate_report(report, fixtures)
    if errors:
        for error in errors:
            print(f"error: {error}", file=sys.stderr)
        return 1
    print("report structure and consistency are valid; this does not prove capability")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
