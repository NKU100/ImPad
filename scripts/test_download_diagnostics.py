import errno
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.request import Request
from urllib.error import URLError

from scripts.ci.fetch_latest_wechat_apk import request_urlopen


class DownloadDiagnosticsTest(unittest.TestCase):
    def test_external_private_addresses_remain_blocked_with_tracing(self):
        rows = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 443))]
        with tempfile.TemporaryDirectory() as temporary, \
                patch.dict(os.environ, {'APK_DOWNLOAD_DIAGNOSTICS': temporary}), \
                patch('socket.getaddrinfo', return_value=rows), \
                patch('socket.socket') as connect, patch('sys.stderr', io.StringIO()):
            with self.assertRaisesRegex(ValueError, 'public'):
                request_urlopen(Request('https://private.example/app.apk'), 1, True)
            connect.assert_not_called()
            events = [json.loads(line) for p in Path(temporary).glob('*.jsonl')
                      for line in p.read_text().splitlines()]
            self.assertIn('request_error', [row['event'] for row in events])

    def test_real_response_and_body_stall_are_distinguished(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header('Content-Length', '20')
                self.end_headers()
                self.wfile.write(b'partial')
                self.wfile.flush()
                time.sleep(0.2)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as temporary, \
                    patch.dict(os.environ, {'APK_DOWNLOAD_DIAGNOSTICS': temporary}), \
                    patch('sys.stderr', io.StringIO()):
                request = Request(f'http://127.0.0.1:{server.server_port}/app.apk?token=secret')
                with request_urlopen(request, 0.1, False) as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.read1(1024), b'partial')
                    with self.assertRaises(TimeoutError):
                        response.read1(1024)
                events = [json.loads(line) for p in Path(temporary).glob('*.jsonl')
                          for line in p.read_text().splitlines()]
                kinds = [row['event'] for row in events]
                self.assertIn('tcp_complete', kinds)
                self.assertIn('http_headers_complete', kinds)
                error = next(row for row in events if row['event'] == 'body_error')
                self.assertEqual(error['bytes'], 7)
                self.assertGreater(error['idleSeconds'], 0)
                self.assertNotIn('secret', json.dumps(events))
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_tls_failure_is_recorded_before_http(self):
        server = socket.socket()
        server.bind(('127.0.0.1', 0))
        server.listen()
        try:
            with tempfile.TemporaryDirectory() as temporary, \
                    patch.dict(os.environ, {'APK_DOWNLOAD_DIAGNOSTICS': temporary}), \
                    patch('ssl.SSLContext.wrap_socket', side_effect=TimeoutError('handshake timeout')), \
                    patch('sys.stderr', io.StringIO()):
                with self.assertRaises(URLError):
                    request_urlopen(Request(f'https://127.0.0.1:{server.getsockname()[1]}/app.apk'), 1, False)
                events = [json.loads(line) for p in Path(temporary).glob('*.jsonl')
                          for line in p.read_text().splitlines()]
                kinds = [row['event'] for row in events]
                self.assertIn('tcp_complete', kinds)
                self.assertIn('tls_error', kinds)
                self.assertNotIn('http_headers_start', kinds)
        finally:
            server.close()

    def test_preserves_every_connection_failure_without_leaking_query(self):
        rows = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('203.0.113.1', 443)),
                (socket.AF_INET6, socket.SOCK_STREAM, 6, '', ('2001:db8::1', 443, 0, 0))]
        class FailedSocket:
            def __init__(self, family, *args):
                self.family = family
            def settimeout(self, timeout):
                pass
            def connect(self, address):
                if self.family == socket.AF_INET:
                    raise TimeoutError('timed out')
                raise OSError(errno.ENETUNREACH, 'Network is unreachable')
            def close(self):
                pass

        with tempfile.TemporaryDirectory() as temporary, \
                patch.dict(os.environ, {'APK_DOWNLOAD_DIAGNOSTICS': temporary}), \
                patch('socket.getaddrinfo', return_value=rows), \
                patch('socket.socket', FailedSocket), patch('sys.stderr', io.StringIO()) as log:
            with self.assertRaises(OSError):
                request_urlopen(Request('https://download.example/app.apk?token=secret'), 30, False)
            files = list(Path(temporary).glob('*.jsonl'))
            self.assertTrue(files, 'Network errors must leave a persistent diagnostic')
            events = [json.loads(line) for p in files for line in p.read_text().splitlines()]
            failures = [e for e in events if e['event'] == 'tcp_error']
            self.assertEqual([e['errorType'] for e in failures], ['TimeoutError', 'OSError'])
            self.assertEqual(failures[-1]['errno'], errno.ENETUNREACH)
            self.assertNotIn('secret', ''.join(p.read_text() for p in files) + log.getvalue())

    def test_diagnostic_storage_failure_keeps_original_connection_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            blocked = Path(temporary) / 'file'
            blocked.write_text('not a directory')
            with patch.dict(os.environ, {'APK_DOWNLOAD_DIAGNOSTICS': str(blocked)}), \
                    patch('socket.getaddrinfo', side_effect=socket.gaierror(-2, 'not found')), \
                    patch('sys.stderr', io.StringIO()):
                with self.assertRaises(OSError) as caught:
                    request_urlopen(Request('https://download.example/app.apk'), 1, False)
                self.assertIn('not found', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
