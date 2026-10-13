"""Validate the signed delivery route returned by QQ's official APK endpoint."""
import re
from urllib.parse import parse_qs, urlsplit

from scripts.ci.discover_latest_wechat import validate_official_apk_url

PREFIX = 'https://downv6.qq.com/qqweb/QQ_1/android_apk/'


def valid_download_redirect(url, source_url):
    if not isinstance(source_url, str):
        return False
    try:
        validate_official_apk_url(source_url, PREFIX)
        source = urlsplit(source_url)
        target = urlsplit(url)
        query = parse_qs(target.query, keep_blank_values=True)
        required = {'mkey', 'cip', 'proto', 'tx_domain', 'tx_path', 'tx_id'}
        return (
            target.scheme == 'https'
            and re.fullmatch(r'[a-f0-9]{32}\.rdt\.tfogc\.com', target.hostname or '') is not None
            and target.port in (None, 443, 49156) and target.username is None and target.password is None
            and not target.fragment
            and target.path == '/' + source.hostname + source.path
            and required <= set(query) <= required | {'access_type'}
            and all(len(values) == 1 for values in query.values())
            and all(query[key][0] for key in required)
            and query.get('access_type', ['']) == ['']
            and query['proto'] == ['https']
            and query['tx_domain'] == ['down.qq.com']
            and query['tx_path'] == ['/qqweb/']
        )
    except (ValueError, TypeError):
        return False
