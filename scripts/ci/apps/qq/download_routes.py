"""Resolve alternative public IPv4 edges for QQ's official APK host."""
from concurrent.futures import ThreadPoolExecutor
import ipaddress
import json
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from scripts.ci.apps.qq.discovery_policy import MAINLAND_CLIENT_SUBNETS
from scripts.ci.discover_latest_wechat import USER_AGENT


def public_ipv4_answers(payload):
    addresses = []
    for answer in payload.get('Answer') or []:
        if not isinstance(answer, dict) or answer.get('type') != 1:
            continue
        try:
            address = ipaddress.IPv4Address(answer.get('data', ''))
        except (ValueError, TypeError):
            continue
        if address.is_global and str(address) not in addresses:
            addresses.append(str(address))
    return addresses


def resolve_download_routes(host='downv6.qq.com'):
    if host != 'downv6.qq.com':
        raise ValueError('QQ edge resolution is restricted to the official APK host')

    def query(subnet):
        started = time.monotonic()
        result = {'source': 'google-default' if subnet is None else f'google-ecs-{subnet}'}
        parameters = {'name': host, 'type': 'A'}
        if subnet is not None:
            parameters['edns_client_subnet'] = subnet
        request = Request('https://dns.google/resolve?' + urlencode(parameters),
                          headers={'User-Agent': USER_AGENT})
        try:
            with urlopen(request, timeout=5) as response:
                payload = response.read(1_000_001)
            if len(payload) > 1_000_000:
                raise ValueError('Oversized DNS response')
            result['addresses'] = public_ipv4_answers(json.loads(payload))[:4]
        except (OSError, ValueError, AttributeError, TypeError) as error:
            result.update(addresses=[], error=type(error).__name__)
        result['seconds'] = round(time.monotonic() - started, 3)
        return result

    with ThreadPoolExecutor(max_workers=4) as pool:
        return list(pool.map(query, (None, *MAINLAND_CLIENT_SUBNETS)))
