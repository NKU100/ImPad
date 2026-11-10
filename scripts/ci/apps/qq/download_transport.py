"""Bounded IPv4 edge failover for the official QQ APK endpoint."""
from http.client import HTTPException
import ipaddress
import os
import socket
import time
from urllib.error import HTTPError
from urllib.request import getproxies, proxy_bypass

from scripts.ci.apps.qq.download_policy import PREFIX
from scripts.ci.apps.qq.download_routes import edge_opener, resolve_download_routes
from scripts.ci.discover_latest_wechat import validate_official_apk_url
from scripts.ci.download_diagnostics import error_fields


def enabled_for_url(url):
    if os.environ.get('QQ_APK_EDGE_FALLBACK') != '1':
        return False
    try:
        validate_official_apk_url(url, PREFIX)
    except (ValueError, TypeError):
        return False
    return not (getproxies().get('https') and not proxy_bypass('downv6.qq.com'))


def transient(error):
    if isinstance(error, HTTPError):
        return error.code in (408, 429, 500, 502, 503, 504)
    return isinstance(error, (OSError, HTTPException))


class QQDownloadTransport:
    def __init__(self):
        self.preferred = None
        self.failed = set()
        self.alternatives = None
        self.updated = time.monotonic()

    def reject_preferred(self):
        if self.preferred:
            self.failed.add(self.preferred)
            self.preferred = None
            self.updated = time.monotonic()

    def open(self, request, timeout, trace=None):
        if time.monotonic() - self.updated > 60:
            self.__init__()
        emit = trace.emit if trace else lambda *args, **kwargs: None
        queue = [self.preferred] if self.preferred else []
        if not queue:
            resolve = trace.resolve if trace else socket.getaddrinfo
            try:
                rows = resolve('downv6.qq.com', 443, socket.AF_INET, socket.SOCK_STREAM)
                queue = list(dict.fromkeys(row[4][0] for row in rows
                                          if ipaddress.IPv4Address(row[4][0]).is_global))[:1]
            except OSError as error:
                emit('qq_primary_dns_error', **error_fields(error))
        seen = set(self.failed)
        loaded_alternatives = False
        last_error = None
        deadline = time.monotonic() + 90
        attempts = 0
        while attempts < 6 and time.monotonic() < deadline:
            if not queue:
                if loaded_alternatives:
                    break
                if self.alternatives is None:
                    sources = resolve_download_routes()
                    # Try regional answers before another copy of the default CDN answer set.
                    sources.sort(key=lambda source: source['source'] == 'google-default')
                    self.alternatives = list(dict.fromkeys(
                        address for source in sources for address in source['addresses']
                        if ipaddress.IPv4Address(address).is_global))
                queue = list(self.alternatives)
                loaded_alternatives = True
                emit('qq_alternative_routes', addresses=queue)
                continue
            address = queue.pop(0)
            if address in seen:
                continue
            seen.add(address)
            attempts += 1
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            emit('qq_route_start', address=address, attempt=attempts)
            try:
                response = edge_opener(address, trace=trace).open(request, timeout=min(6, remaining))
            except Exception as error:
                if not transient(error):
                    raise
                last_error = error
                if isinstance(error, HTTPError):
                    error.close()
                self.failed.add(address)
                emit('qq_route_error', address=address, **error_fields(error))
                continue
            # Connection/TLS/header attempts stay short; allow longer idle periods for APK data.
            sock = getattr(getattr(getattr(response, 'fp', None), 'raw', None), '_sock', None)
            if sock is not None:
                sock.settimeout(min(timeout, 30))
            self.preferred = address
            emit('qq_route_selected', address=address)
            return response
        if last_error is not None:
            raise last_error
        raise OSError('No reachable public IPv4 route for the official QQ APK')


transport = QQDownloadTransport()
