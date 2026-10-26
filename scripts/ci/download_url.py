import ipaddress
import socket
import string
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler


def validate_download_url(url: str) -> None:
    if not isinstance(url, str) or any(char in string.whitespace or ord(char) < 32 or ord(char) == 127 for char in url):
        raise ValueError('Download URL contains invalid whitespace or control characters')
    try:
        parsed = urlsplit(url)
        _ = parsed.port
    except ValueError as error:
        raise ValueError('Invalid download URL or port') from error
    host = parsed.hostname
    if (parsed.scheme.lower() != 'https' or not host or parsed.username is not None
            or parsed.password is not None or parsed.fragment or '\\' in parsed.netloc
            or '%' in parsed.netloc or host.endswith('.')):
        raise ValueError('Download URL must be public HTTPS without credentials or fragment')
    if host.lower() == 'localhost' or host.lower().endswith('.localhost'):
        raise ValueError('Download URL must use a public host')
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        labels = host.rstrip('.').split('.')
        if (not all(labels) or any(len(label) > 63 or label.startswith('-') or label.endswith('-')
                                   or not all(ch.isalnum() or ch == '-' for ch in label) for label in labels)):
            raise ValueError('Download URL contains an invalid host')
    else:
        if not address.is_global:
            raise ValueError('Download host must be a public address')


def validate_download_target(url: str) -> None:
    validate_download_url(url)
    parsed = urlsplit(url)
    host = parsed.hostname
    try:
        ipaddress.ip_address(host)
        return
    except ValueError:
        pass
    try:
        addresses = {item[4][0].split('%', 1)[0] for item in socket.getaddrinfo(
            host, parsed.port or 443, type=socket.SOCK_STREAM
        )}
    except OSError as error:
        raise ValueError('Download host could not be resolved to a public address') from error
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise ValueError('Download host must resolve only to public addresses')


class SafeRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_download_target(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)
