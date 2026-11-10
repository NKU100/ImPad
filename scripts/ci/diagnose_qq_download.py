"""Diagnose QQ download connectivity with bounded APK transfer samples."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import time
import traceback
from urllib.parse import urlsplit
from urllib.request import Request
from unittest.mock import patch

from scripts.ci.app_policy import get_policy
from scripts.ci.apps.qq.download_policy import valid_download_redirect
from scripts.ci.apps.qq.download_routes import edge_opener, resolve_download_routes
from scripts.ci.discover_latest_wechat import USER_AGENT, validate_official_apk_url
from scripts.ci.fetch_latest_wechat_apk import remote_metadata


def original_request(url):
    started = time.monotonic()

    def event(kind, **fields):
        print(json.dumps({'elapsed': round(time.monotonic() - started, 3),
                          'event': kind, **fields}), flush=True)

    original_resolve = socket.getaddrinfo
    original_socket = socket.socket

    def resolve(*args, **kwargs):
        begin = time.monotonic()
        try:
            rows = original_resolve(*args, **kwargs)
        except OSError as error:
            event('dns-error', host=args[0], error=repr(error))
            raise
        event('dns', host=args[0], seconds=round(time.monotonic() - begin, 3),
              addresses=[{'family': row[0].name, 'address': row[4]} for row in rows])
        return rows

    class TracedSocket(original_socket):
        def connect(self, address):
            begin = time.monotonic()
            event('tcp-start', family=self.family.name, address=address)
            try:
                result = super().connect(address)
            except OSError as error:
                event('tcp-error', address=address, error=repr(error),
                      seconds=round(time.monotonic() - begin, 3))
                raise
            event('tcp-connected', address=address,
                  seconds=round(time.monotonic() - begin, 3))
            return result

    with patch.object(socket, 'getaddrinfo', resolve), patch.object(socket, 'socket', TracedSocket):
        try:
            event('head-success', metadata=remote_metadata(url, get_policy('qq'), allow_external=True))
        except Exception as error:
            event('head-error', error=repr(error))
            traceback.print_exc()
            return 1
    return 0


def probe_address(row, url):
    family, socktype, proto, _, address = row
    parsed = urlsplit(url)
    result = {'family': family.name, 'address': address, 'stage': 'TCP', 'timings': {}}
    sock = None
    started = time.monotonic()
    try:
        sock = socket.socket(family, socktype, proto)
        sock.settimeout(6)
        sock.connect(address)
        result['timings']['tcp'] = round(time.monotonic() - started, 3)
        result['stage'] = 'TLS'
        begin = time.monotonic()
        sock = ssl.create_default_context().wrap_socket(sock, server_hostname=parsed.hostname)
        result['timings']['tls'] = round(time.monotonic() - begin, 3)
        result['stage'] = 'HTTP'
        begin = time.monotonic()
        request = (f'HEAD {parsed.path} HTTP/1.1\r\nHost: {parsed.hostname}\r\n'
                   f'User-Agent: {USER_AGENT}\r\nConnection: close\r\n\r\n')
        sock.sendall(request.encode('ascii'))
        response = b''
        while b'\r\n\r\n' not in response and len(response) < 65536:
            block = sock.recv(4096)
            if not block:
                break
            response += block
        result['timings']['http'] = round(time.monotonic() - begin, 3)
        result['headers'] = response.decode('iso-8859-1')
        result['statusLine'] = result['headers'].split('\r\n', 1)[0]
        result['stage'] = 'COMPLETE' if b'\r\n\r\n' in response else 'HTTP'
        if result['stage'] != 'COMPLETE':
            result['error'] = 'Incomplete HTTP response headers'
    except Exception as error:
        result['error'] = repr(error)
    finally:
        if sock is not None:
            sock.close()
        result['timings']['total'] = round(time.monotonic() - started, 3)
    print(json.dumps(result), flush=True)
    return result


def probe_download_route(item, url):
    address, sources = item
    parsed = urlsplit(url)
    result = {'address': address, 'sources': sources, 'stage': 'HEAD'}
    started = time.monotonic()
    try:
        opener = edge_opener(address, parsed.hostname)
        with opener.open(Request(url, headers={'User-Agent': USER_AGENT}, method='HEAD'), timeout=6) as response:
            result['headStatus'] = response.status
            result['headSeconds'] = round(time.monotonic() - started, 3)
            if response.status != 200:
                result['stage'] = 'HEAD_REJECTED'
                return result
        result['stage'] = 'GET'
        with opener.open(Request(url, headers={
                'User-Agent': USER_AGENT, 'Range': 'bytes=0-65535'}), timeout=6) as response:
            result['getStatus'] = response.status
            result['redirected'] = response.geturl() != url
            if result['redirected'] and not valid_download_redirect(response.geturl(), url):
                result['stage'] = 'UNTRUSTED_REDIRECT'
                return result
            if response.status not in (200, 206):
                result['stage'] = 'GET_REJECTED'
                return result
            data = response.read(65536)
        result['bytes'] = len(data)
        result['sampleSha256'] = hashlib.sha256(data).hexdigest()
        result['apkZipHeader'] = data.startswith(b'PK\x03\x04')
        result['stage'] = 'TRANSFER_CONFIRMED' if len(data) == 65536 and result['apkZipHeader'] else 'INVALID_SAMPLE'
    except Exception as error:
        result['error'] = repr(error)
    finally:
        result['seconds'] = round(time.monotonic() - started, 3)
        print(json.dumps(result), flush=True)
    return result


def diagnose(url, directory):
    directory.mkdir(parents=True, exist_ok=True)
    baseline = [sys.executable, '-u', '-m', 'scripts.ci.diagnose_qq_download',
                '--url', url, '--original-request']
    with (directory / 'original-request.log').open('w') as log:
        try:
            code = subprocess.run(baseline, stdout=log, stderr=subprocess.STDOUT, timeout=100).returncode
            baseline_status = f'exit {code}'
        except subprocess.TimeoutExpired:
            baseline_status = 'Stopped after 100s; see partial trace'
    print((directory / 'original-request.log').read_text(), flush=True)
    parsed = urlsplit(url)
    started = time.monotonic()
    try:
        rows = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
        rows = list({(row[0], row[4]): row for row in rows}.values())
        dns = {'seconds': round(time.monotonic() - started, 3),
               'addresses': [{'family': row[0].name, 'address': row[4]} for row in rows]}
    except OSError as error:
        rows = []
        dns = {'seconds': round(time.monotonic() - started, 3), 'error': repr(error)}
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda row: probe_address(row, url), rows))
    alternative_dns = resolve_download_routes(parsed.hostname)
    candidates = {}
    for source in alternative_dns:
        for address in source['addresses']:
            candidates.setdefault(address, []).append(source['source'])
    with ThreadPoolExecutor(max_workers=8) as pool:
        transfers = list(pool.map(lambda item: probe_download_route(item, url), candidates.items()))
    curl_results = []
    for label, flags in [('default', []), ('ipv4', ['-4']), ('ipv6', ['-6'])]:
        command = ['curl', *flags, '--head', '--location', '--max-redirs', '5',
                   '--proto', '=https', '--proto-redir', '=https', '--verbose',
                   '--connect-timeout', '6', '--max-time', '20',
                   '--user-agent', USER_AGENT, '--write-out',
                   '\nremote_ip=%{remote_ip} dns=%{time_namelookup} tcp=%{time_connect}'
                   ' tls=%{time_appconnect} first_byte=%{time_starttransfer} total=%{time_total}\n', url]
        with (directory / f'curl-{label}.log').open('w') as log:
            try:
                code = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, timeout=25).returncode
                curl_results.append({'mode': label, 'exit': code})
            except subprocess.TimeoutExpired:
                curl_results.append({'mode': label, 'error': 'Process exceeded 25s'})
    report = {'url': url, 'originalRequest': baseline_status, 'dns': dns,
              'addresses': results, 'curl': curl_results,
              'alternativeDns': alternative_dns, 'alternativeTransfers': transfers}
    (directory / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
    summary = ['# QQ download network diagnosis', '', f'URL: `{url}`', '',
               f'Original Python HEAD: **{baseline_status}**; full DNS/TCP trace in artifact.', '',
               f'DNS: `{json.dumps(dns)}`', '',
               '| Address | Family | Last stage | Seconds | Result |',
               '| --- | --- | --- | --- | --- |']
    for row in results:
        outcome = row.get('error', row.get('statusLine', '')).replace('|', '\\|')
        summary.append(f"| {row['address'][0]} | {row['family']} | {row['stage']} | "
                       f"{row['timings']['total']} | {outcome} |")
    summary += ['', '## Alternative DNS and bounded APK transfer', '',
                'Each route keeps the official hostname and TLS verification and reads at most 64 KiB.', '',
                '| Address | DNS sources | Result | Seconds |',
                '| --- | --- | --- | --- |']
    for row in transfers:
        outcome = row.get('error', row['stage']).replace('|', '\\|')
        summary.append(f"| {row['address']} | {', '.join(row['sources'])} | {outcome} | {row['seconds']} |")
    summary += ['', 'curl comparison: `' + json.dumps(curl_results) + '`', '',
                'A completed diagnostic does not imply that download connectivity passed. '
                'Per-address HEAD probes keep TLS hostname verification and do not follow redirects; '
                'the original request and curl comparison also test redirects.']
    (directory / 'summary.md').write_text('\n'.join(summary) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', required=True)
    parser.add_argument('--directory', type=Path, default=Path('work/qq-download-diagnostic'))
    parser.add_argument('--original-request', action='store_true')
    args = parser.parse_args()
    validate_official_apk_url(args.url, get_policy('qq').official_apk_prefix)
    if args.original_request:
        return original_request(args.url)
    diagnose(args.url, args.directory)
    return 0


if __name__ == '__main__':
    sys.exit(main())
