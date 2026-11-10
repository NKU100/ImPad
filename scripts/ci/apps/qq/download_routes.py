"""Resolve alternative public IPv4 edges for QQ's official APK host."""
from concurrent.futures import ThreadPoolExecutor
import ipaddress
import http.client
import json
from pathlib import Path
import time
import socket
import subprocess
import sys
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen, HTTPSHandler, build_opener

from scripts.ci.apps.qq.discovery_policy import MAINLAND_CLIENT_SUBNETS
from scripts.ci.discover_latest_wechat import USER_AGENT
from scripts.ci.download_url import SafeRedirectHandler


def edge_opener(address, host='downv6.qq.com', trace=None):
    if not ipaddress.IPv4Address(address).is_global:
        raise ValueError('QQ edge address must be public IPv4')
    base = http.client.HTTPSConnection
    if trace is not None:
        from scripts.ci.download_diagnostics import connection_class
        base = connection_class(base, trace)

    class EdgeHandler(HTTPSHandler):
        def https_open(self, request):
            def connection(target_host, **kwargs):
                result = base(target_host, **kwargs)
                connect = trace.connect if trace is not None else socket.create_connection

                def pinned(target, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None):
                    destination = (address, target[1]) if target[0] == host else target
                    return connect(destination, timeout, source_address)

                result._create_connection = pinned
                return result
            return self.do_open(connection, request, context=self._context)

    return build_opener(EdgeHandler(), SafeRedirectHandler(diagnostics=trace))


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


def _resolve_download_routes(host):
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


def _resolver_process(mode, host, timeout):
    if host != 'downv6.qq.com':
        raise ValueError('QQ edge resolution is restricted to the official APK host')
    # A socket timeout does not bound DNS or slow streaming DoH responses; kill the worker instead.
    completed = subprocess.run(
        [sys.executable, '-m', 'scripts.ci.apps.qq.download_routes', mode, host],
        cwd=Path(__file__).resolve().parents[4],
        capture_output=True, text=True, check=True, timeout=max(0.001, timeout))
    return json.loads(completed.stdout)


def resolve_download_routes(host='downv6.qq.com', timeout=8):
    try:
        return _resolver_process('--regional', host, timeout)
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        return [{'source': 'alternative-dns', 'addresses': [], 'error': type(error).__name__}]


def system_download_routes(timeout=8):
    return _resolver_process('--system', 'downv6.qq.com', timeout)


if __name__ == '__main__':
    mode, host = sys.argv[1:]
    if host != 'downv6.qq.com':
        raise SystemExit('Unsupported DNS host')
    if mode == '--system':
        rows = socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)
        result = list(dict.fromkeys(row[4][0] for row in rows
                                   if ipaddress.IPv4Address(row[4][0]).is_global))
    elif mode == '--regional':
        result = _resolve_download_routes(host)
    else:
        raise SystemExit('Unsupported DNS mode')
    print(json.dumps(result))
