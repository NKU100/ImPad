import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path

from scripts.ci.app_policy import get_policy
from typing import Optional
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from scripts.ci.discover_latest_wechat import USER_AGENT, append_github_output, validate_official_apk_url


MAX_APK_BYTES = 1_500_000_000
CHUNK_SIZE = 1024 * 1024


def final_url_is_official(url: str, policy, source_url=None) -> bool:
    try:
        validate_official_apk_url(url, policy.official_apk_prefix)
        return True
    except ValueError:
        if policy.app_id == 'qq':
            from scripts.ci.apps.qq.download_policy import valid_download_redirect
            return valid_download_redirect(url, source_url)
        return False


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while True:
            block = source.read(CHUNK_SIZE)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def cache_matches(
    cache_dir: Path,
    url: str,
    content_length: Optional[int],
    last_modified: str,
    etag: str,
    expected_sha256: str,
) -> bool:
    if content_length is None or content_length <= 0 or not (last_modified or etag):
        return False

    metadata_path = cache_dir / "candidate.json"
    if not metadata_path.is_file():
        return False

    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if not isinstance(metadata, dict):
            return False
        cached_sha256 = metadata["sha256"]
        if not re.fullmatch(r"[a-f0-9]{64}", cached_sha256):
            return False
        apk_path = cache_dir / f"{cached_sha256}.apk"
        if metadata.get("url") != url:
            return False
        if metadata.get("content_length") != content_length:
            return False
        if metadata.get("last_modified", "") != last_modified:
            return False
        if metadata.get("etag", "") != etag:
            return False
        if apk_path.stat().st_size != content_length:
            return False
        if expected_sha256 and cached_sha256 != expected_sha256:
            return False
        return file_sha256(apk_path) == cached_sha256
    except (OSError, ValueError, KeyError, TypeError, UnicodeError, json.JSONDecodeError):
        return False


def header_metadata(headers) -> dict[str, object]:
    raw_length = headers.get("Content-Length")
    if raw_length is None:
        content_length = None
    else:
        try:
            content_length = int(raw_length)
        except ValueError as error:
            raise ValueError("Tencent CDN returned an invalid Content-Length") from error
    return {
        "content_length": content_length,
        "last_modified": headers.get("Last-Modified", ""),
        "etag": headers.get("ETag", ""),
    }


def remote_metadata(url: str, policy) -> dict[str, object]:
    request = Request(url, headers={"User-Agent": USER_AGENT}, method="HEAD")
    try:
        with urlopen(request, timeout=30) as response:
            if response.status != 200:
                raise ValueError(f"Tencent CDN HEAD request returned HTTP {response.status}")
            if not final_url_is_official(response.geturl(), policy, url):
                raise ValueError("Tencent CDN HEAD request redirected outside its official APK directory")
            return header_metadata(response.headers)
    except HTTPError as error:
        if error.code in (405, 501):
            return {"content_length": None, "last_modified": "", "etag": ""}
        raise


def write_metadata(path: Path, metadata: dict[str, object]) -> None:
    temporary_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=".candidate-metadata-",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            json.dump(metadata, temporary, sort_keys=True)
            temporary.write("\n")
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def download_candidate(
    url: str,
    cache_dir: Path,
    expected_sha256: str,
    remote: dict[str, object],
    policy,
) -> tuple[Path, str, bool, dict[str, object]]:
    validate_official_apk_url(url, policy.official_apk_prefix)
    if expected_sha256 and not re.fullmatch(r"[a-f0-9]{64}", expected_sha256):
        raise ValueError("Registered APK SHA-256 must contain 64 lowercase hexadecimal characters")

    cache_dir.mkdir(parents=True, exist_ok=True)
    metadata_path = cache_dir / "candidate.json"
    if cache_matches(
        cache_dir,
        url,
        remote.get("content_length"),
        str(remote.get("last_modified", "")),
        str(remote.get("etag", "")),
        expected_sha256,
    ):
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        apk_path = cache_dir / f"{metadata['sha256']}.apk"
        return apk_path, metadata["sha256"], True, remote

    request = Request(url, headers={"User-Agent": USER_AGENT})
    temporary_path: Optional[Path] = None
    digest = hashlib.sha256()
    byte_count = 0
    try:
        with tempfile.NamedTemporaryFile(dir=cache_dir, prefix=".candidate-download-", delete=False) as temporary:
            temporary_path = Path(temporary.name)
            with urlopen(request, timeout=120) as response:
                if response.status != 200:
                    raise ValueError(f"Tencent CDN returned HTTP {response.status}")
                if not final_url_is_official(response.geturl(), policy, url):
                    raise ValueError("Tencent CDN download redirected outside its official APK directory")
                downloaded_headers = header_metadata(response.headers)
                remote_length = remote.get("content_length")
                downloaded_length = downloaded_headers.get("content_length")
                if remote_length is not None and downloaded_length is not None and remote_length != downloaded_length:
                    raise ValueError("APK Content-Length changed between HEAD and download")
                for header in ("last_modified", "etag"):
                    remote_value = remote.get(header)
                    downloaded_value = downloaded_headers.get(header)
                    if remote_value and downloaded_value and remote_value != downloaded_value:
                        raise ValueError(f"APK {header.replace('_', '-')} changed between HEAD and download")
                for block in iter(lambda: response.read(CHUNK_SIZE), b""):
                    byte_count += len(block)
                    if byte_count > MAX_APK_BYTES:
                        raise ValueError("Official WeChat APK exceeded the 1.5 GB safety limit")
                    digest.update(block)
                    temporary.write(block)

        advertised_length = remote.get("content_length")
        if advertised_length is None:
            advertised_length = downloaded_headers.get("content_length")
        if advertised_length is not None and byte_count != advertised_length:
            raise ValueError(f"Downloaded byte count {byte_count} does not match Content-Length {advertised_length}")

        actual_sha256 = digest.hexdigest()
        if expected_sha256 and actual_sha256 != expected_sha256:
            raise ValueError(f"APK SHA-256 mismatch: expected {expected_sha256}, received {actual_sha256}")

        apk_path = cache_dir / f"{actual_sha256}.apk"
        os.replace(temporary_path, apk_path)
        temporary_path = None
        metadata = {
            "url": url,
            "sha256": actual_sha256,
            "content_length": byte_count,
            "last_modified": downloaded_headers.get("last_modified") or remote.get("last_modified", ""),
            "etag": downloaded_headers.get("etag") or remote.get("etag", ""),
        }
        write_metadata(cache_dir / "candidate.json", metadata)
        return apk_path, actual_sha256, False, metadata
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def append_report(path: Path, message: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as report:
        report.write(message)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app-id", required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--cache-dir", type=Path, required=True)
    parser.add_argument("--expected-sha256", default="")
    parser.add_argument("--github-output", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    arguments = parser.parse_args()
    policy = get_policy(arguments.app_id)

    try:
        validate_official_apk_url(arguments.url, policy.official_apk_prefix)
        remote = remote_metadata(arguments.url, policy)
        apk_path, actual_sha256, reused, metadata = download_candidate(
            arguments.url,
            arguments.cache_dir,
            arguments.expected_sha256,
            remote,
            policy,
        )
        append_github_output(arguments.github_output, {
            "apk_path": apk_path.as_posix(),
            "apk_sha256": actual_sha256,
            "cache_hit": str(reused).lower(),
        })
        append_report(arguments.report, "## Official candidate download\n\n")
        append_report(arguments.report, f"APK SHA-256: {actual_sha256}\n\n")
        append_report(arguments.report, f"Cache: {'hit' if reused else 'downloaded'}\n\n")
        append_report(arguments.report, f"Content-Length: {metadata.get('content_length')}\n\n")
        append_report(arguments.report, f"Last-Modified: {metadata.get('last_modified') or 'unavailable'}\n\n")
        print(f"Candidate APK SHA-256: {actual_sha256}; cache {'hit' if reused else 'miss'}")
        return 0
    except Exception as error:
        message = f"Official candidate download/cache validation failed: {error}"
        print(message, file=sys.stderr)
        append_report(arguments.report, f"\nDownload or cache validation failed: {error}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
