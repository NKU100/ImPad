"""Discover the ARM64 APK from the configuration used by QQ's official mobile page."""
from concurrent.futures import ThreadPoolExecutor, wait
import http.client
import ipaddress
import json
import re
import socket
import ssl
import sys
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from scripts.ci.discover_latest_wechat import Candidate, USER_AGENT, validate_official_apk_url

OFFICIAL_CONFIG = 'https://qq-web.cdn-go.cn/im.qq.com_new/latest/rainbow/mobileConfig.json'
MAX_CONFIG_BYTES = 1_000_000
DOH_RESOLVER = 'https://dns.google/resolve'
DOH_TIMEOUT_SECONDS = 5
# The resolver host has several addresses and urllib waits for each one, so the whole lookup
# needs its own deadline to keep an unreachable resolver from delaying discovery.
EDGE_LOOKUP_DEADLINE_SECONDS = 10
# Overseas edges of the configuration CDN keep serving releases that are weeks old and ignore
# cache-busting queries; edges resolved for mainland client subnets follow the origin.
MAINLAND_CLIENT_SUBNETS = ('183.60.0.0/16', '113.108.0.0/16', '202.96.0.0/16')
FILENAME_PATTERN = re.compile(r'(\d+\.\d+\.\d+)\.(\d+)_[a-fA-F0-9]+\.apk')


def discover_from_config(config, official_apk_prefix):
    android = config.get('android', {})
    if not isinstance(android, dict):
        raise ValueError('Official QQ configuration has no Android release')
    url = android.get('x64Link', '')
    validate_official_apk_url(url, official_apk_prefix)
    filename = urlsplit(url).path.rsplit('/', 1)[-1]
    match = FILENAME_PATTERN.fullmatch(filename)
    if match is None or match.group(1) != android.get('version'):
        raise ValueError('Official QQ ARM64 download does not match its declared version')
    # The filename contains QQ's build number; versionCode is inspected from the APK later.
    build = int(match.group(2))
    return Candidate(url, filename, build, build)


def release_rank(candidate):
    match = FILENAME_PATTERN.fullmatch(candidate.filename)
    return tuple(int(part) for part in match.group(1).split('.')), candidate.build_number


def read_config(response):
    if response.status != 200:
        raise ValueError(f'Official QQ configuration returned HTTP {response.status}')
    body = response.read(MAX_CONFIG_BYTES + 1)
    if len(body) > MAX_CONFIG_BYTES:
        raise ValueError('Official QQ configuration is oversized')
    config = json.loads(body)
    if not isinstance(config, dict):
        raise ValueError('Official QQ configuration is not an object')
    return config


def fetch_config():
    with urlopen(Request(OFFICIAL_CONFIG, headers={'User-Agent': USER_AGENT}), timeout=30) as response:
        if response.geturl() != OFFICIAL_CONFIG:
            raise ValueError('Official QQ configuration redirected')
        return read_config(response)


class EdgeConnection(http.client.HTTPSConnection):
    """Connect to one CDN edge address while verifying TLS for the official host name."""

    def __init__(self, host, address, timeout):
        self.tls_context = ssl.create_default_context()
        super().__init__(host, timeout=timeout, context=self.tls_context)
        self.address = address

    def connect(self):
        connection = socket.create_connection((self.address, self.port), self.timeout)
        self.sock = self.tls_context.wrap_socket(connection, server_hostname=self.host)


def fetch_config_from_edge(address):
    parsed = urlsplit(OFFICIAL_CONFIG)
    connection = EdgeConnection(parsed.hostname, address, timeout=30)
    try:
        connection.request('GET', parsed.path, headers={'User-Agent': USER_AGENT})
        return read_config(connection.getresponse())
    finally:
        connection.close()


def resolve_for_subnet(subnet):
    host = urlsplit(OFFICIAL_CONFIG).hostname
    query = urlencode({'name': host, 'type': 'A', 'edns_client_subnet': subnet})
    request = Request(f'{DOH_RESOLVER}?{query}', headers={'User-Agent': USER_AGENT})
    try:
        with urlopen(request, timeout=DOH_TIMEOUT_SECONDS) as response:
            answers = json.loads(response.read(MAX_CONFIG_BYTES)).get('Answer') or []
    except (OSError, ValueError, AttributeError) as error:
        print(f'QQ mainland edge lookup for {subnet} failed: {error}', file=sys.stderr)
        return []
    addresses = []
    for answer in answers:
        address = answer.get('data', '') if isinstance(answer, dict) else ''
        try:
            ipaddress.IPv4Address(address)
        except ValueError:
            continue
        addresses.append(address)
    return addresses


def mainland_edge_addresses():
    executor = ThreadPoolExecutor(max_workers=len(MAINLAND_CLIENT_SUBNETS))
    lookups = [executor.submit(resolve_for_subnet, subnet) for subnet in MAINLAND_CLIENT_SUBNETS]
    executor.shutdown(wait=False)
    _, pending = wait(lookups, timeout=EDGE_LOOKUP_DEADLINE_SECONDS)
    if pending:
        print(f'QQ mainland edge lookup exceeded {EDGE_LOOKUP_DEADLINE_SECONDS}s for '
              f'{len(pending)} subnet(s)', file=sys.stderr)
    addresses = []
    for lookup in lookups:
        if not lookup.done():
            continue
        for address in lookup.result():
            if address not in addresses:
                addresses.append(address)
                break
    return addresses


def discover(policy):
    sources = [('default resolution', fetch_config)]
    sources += [(f'mainland edge {address}', lambda address=address: fetch_config_from_edge(address))
                for address in mainland_edge_addresses()]
    candidates = []
    for label, fetch in sources:
        try:
            candidate = discover_from_config(fetch(), policy.official_apk_prefix)
        except (OSError, ValueError, http.client.HTTPException) as error:
            print(f'QQ configuration from {label} failed: {error}', file=sys.stderr)
            continue
        print(f'QQ configuration from {label}: {candidate.filename}', file=sys.stderr)
        candidates.append(candidate)
    if not candidates:
        raise ValueError('No official QQ configuration source returned a valid release')
    highest = max(release_rank(candidate) for candidate in candidates)
    latest = {candidate.url: candidate for candidate in candidates if release_rank(candidate) == highest}
    if len(latest) != 1:
        raise ValueError('Official QQ configuration sources disagree on the latest download: '
                         + ', '.join(sorted(item.filename for item in latest.values())))
    return next(iter(latest.values()))
