"""Discover the ARM64 APK from the configuration used by QQ's official mobile page."""
import json
import re
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from scripts.ci.discover_latest_wechat import Candidate, USER_AGENT, validate_official_apk_url

OFFICIAL_CONFIG = 'https://qq-web.cdn-go.cn/im.qq.com_new/latest/rainbow/mobileConfig.json'
MAX_CONFIG_BYTES = 1_000_000


def discover_from_config(config, official_apk_prefix):
    android = config.get('android', {})
    url = android.get('x64Link', '')
    validate_official_apk_url(url, official_apk_prefix)
    filename = urlsplit(url).path.rsplit('/', 1)[-1]
    match = re.fullmatch(r'(\d+\.\d+\.\d+)\.(\d+)_[a-fA-F0-9]+\.apk', filename)
    if match is None or match.group(1) != android.get('version'):
        raise ValueError('Official QQ ARM64 download does not match its declared version')
    # The filename contains QQ's build number; versionCode is inspected from the APK later.
    build = int(match.group(2))
    return Candidate(url, filename, build, build)


def discover(policy):
    with urlopen(Request(OFFICIAL_CONFIG, headers={'User-Agent': USER_AGENT}), timeout=30) as response:
        if response.status != 200 or response.geturl() != OFFICIAL_CONFIG:
            raise ValueError('Official QQ configuration redirected or returned an error')
        body = response.read(MAX_CONFIG_BYTES + 1)
    if len(body) > MAX_CONFIG_BYTES:
        raise ValueError('Official QQ configuration is oversized')
    return discover_from_config(json.loads(body), policy.official_apk_prefix)
