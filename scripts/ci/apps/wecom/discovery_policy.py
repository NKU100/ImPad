"""Discover the current WeCom Android APK from Tencent's official download page."""
from html.parser import HTMLParser
import re
from urllib.parse import parse_qs, urljoin, urlsplit
from urllib.request import Request, urlopen

from scripts.ci.discover_latest_wechat import Candidate, USER_AGENT, validate_official_apk_url


OFFICIAL_PAGE = "https://work.weixin.qq.com/"
OFFICIAL_HOST = "work.weixin.qq.com"
DOWNLOAD_PATH = "/wework_admin/commdownload"
MAX_PAGE_BYTES = 5_000_000
APK_FILENAME_PATTERN = re.compile(
    r"WeCom_android_(?P<version>\d+\.\d+\.\d+)\.(?P<version_code>\d+)"
    r"_arm64(?:_[A-Za-z0-9._-]+)?\.apk",
    re.IGNORECASE,
)


class _DownloadLinkParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.android_links = set()

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "a":
            return
        href = dict(attrs).get("href")
        if not href:
            return
        absolute_url = urljoin(OFFICIAL_PAGE, href)
        parsed = urlsplit(absolute_url)
        try:
            port = parsed.port
        except ValueError:
            return
        query = parse_qs(parsed.query, keep_blank_values=True)
        if (
            parsed.scheme == "https"
            and parsed.hostname == OFFICIAL_HOST
            and parsed.username is None
            and parsed.password is None
            and port is None
            and parsed.path == DOWNLOAD_PATH
            and parsed.fragment == ""
            and query.get("platform") == ["android"]
        ):
            self.android_links.add(absolute_url)


def fetch_official_page():
    request = Request(OFFICIAL_PAGE, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=30) as response:
        final_url = urlsplit(response.geturl())
        if final_url.scheme != "https" or final_url.hostname != OFFICIAL_HOST:
            raise ValueError("Official WeCom download page redirected outside work.weixin.qq.com")
        if response.status != 200:
            raise ValueError(f"Official WeCom download page returned HTTP {response.status}")
        page = response.read(MAX_PAGE_BYTES + 1)
    if len(page) > MAX_PAGE_BYTES:
        raise ValueError("Official WeCom download page exceeded the 5 MB safety limit")
    return page.decode("utf-8", errors="replace")


def validate_resolved_url(url, official_apk_prefix):
    validate_official_apk_url(url, official_apk_prefix)


def resolve_download_url(url, official_apk_prefix):
    request = Request(url, headers={"User-Agent": USER_AGENT}, method="HEAD")
    with urlopen(request, timeout=30) as response:
        final_url = response.geturl()
        if response.status != 200:
            raise ValueError(f"Official WeCom download endpoint returned HTTP {response.status}")
    validate_resolved_url(final_url, official_apk_prefix)
    return final_url


def discover_from_html(page_html, official_apk_prefix):
    parser = _DownloadLinkParser()
    parser.feed(page_html)
    if not parser.android_links:
        raise ValueError("Official WeCom page contains no Android download link")
    if len(parser.android_links) != 1:
        raise ValueError("Official WeCom page contains ambiguous Android download links")

    url = resolve_download_url(next(iter(parser.android_links)), official_apk_prefix)
    filename = urlsplit(url).path.rsplit("/", 1)[-1]
    match = APK_FILENAME_PATTERN.fullmatch(filename)
    if match is None:
        raise ValueError("Official WeCom APK filename does not identify an ARM64 version")
    version_code = int(match.group("version_code"))
    return Candidate(url, filename, version_code, version_code)


def discover(policy):
    return discover_from_html(fetch_official_page(), policy.official_apk_prefix)
