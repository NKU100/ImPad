"""Opt-in, request-scoped network evidence for the shared APK downloader."""
from datetime import datetime, timezone
import http.client
import json
import os
from pathlib import Path
import socket
import sys
import time
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPHandler, HTTPSHandler
import uuid


def public_url(url):
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc.rsplit('@', 1)[-1], parsed.path, '', ''))


def error_fields(error):
    fields = {'errorType': type(error).__name__}
    if isinstance(error, OSError) and isinstance(error.errno, int):
        fields['errno'] = error.errno
    reason = getattr(error, 'reason', None)
    if isinstance(reason, BaseException):
        fields['cause'] = error_fields(reason)
    return fields


class DownloadTrace:
    def __init__(self, request):
        self.started = time.monotonic()
        self.request_id = uuid.uuid4().hex
        self.path = Path(os.environ['APK_DOWNLOAD_DIAGNOSTICS']) / f'{self.request_id}.jsonl'
        self.emit('request_start', url=public_url(request.full_url), method=request.get_method())

    def emit(self, event, **fields):
        row = {'time': datetime.now(timezone.utc).isoformat(), 'requestId': self.request_id,
               'elapsed': round(time.monotonic() - self.started, 3), 'event': event, **fields}
        line = json.dumps(row, ensure_ascii=True)
        try:
            print('APK network: ' + line, file=sys.stderr, flush=True)
        except OSError:
            pass
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open('a') as output:
                output.write(line + '\n')
        except OSError:
            pass

    def resolve(self, *args, **kwargs):
        started = time.monotonic()
        self.emit('dns_start', host=args[0])
        try:
            rows = socket.getaddrinfo(*args, **kwargs)
        except OSError as error:
            self.emit('dns_error', seconds=round(time.monotonic() - started, 3), **error_fields(error))
            raise
        self.emit('dns_complete', seconds=round(time.monotonic() - started, 3),
                  addresses=[{'family': int(row[0]), 'address': row[4]} for row in rows])
        return rows

    def connect(self, address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None):
        # Match socket.create_connection's order and last-error behavior while retaining every failure.
        last_error = None
        for family, kind, proto, _, target in self.resolve(address[0], address[1], 0, socket.SOCK_STREAM):
            sock = None
            started = time.monotonic()
            self.emit('tcp_start', family=int(family), address=target)
            try:
                sock = socket.socket(family, kind, proto)
                if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                    sock.settimeout(timeout)
                if source_address:
                    sock.bind(source_address)
                sock.connect(target)
                self.emit('tcp_complete', address=target, seconds=round(time.monotonic() - started, 3))
                return sock
            except OSError as error:
                last_error = error
                self.emit('tcp_error', address=target, seconds=round(time.monotonic() - started, 3),
                          **error_fields(error))
                if sock is not None:
                    sock.close()
        if last_error is not None:
            raise last_error
        raise OSError('getaddrinfo returns an empty list')


class TracedTLSContext:
    def __init__(self, context, trace):
        self.context = context
        self.trace = trace

    def __getattr__(self, name):
        return getattr(self.context, name)

    def wrap_socket(self, *args, **kwargs):
        started = time.monotonic()
        self.trace.emit('tls_start', host=kwargs.get('server_hostname'))
        try:
            sock = self.context.wrap_socket(*args, **kwargs)
        except Exception as error:
            self.trace.emit('tls_error', seconds=round(time.monotonic() - started, 3), **error_fields(error))
            raise
        self.trace.emit('tls_complete', seconds=round(time.monotonic() - started, 3),
                        version=sock.version(), cipher=sock.cipher())
        return sock


def connection_class(base, trace):
    class TracedConnection(base):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._create_connection = trace.connect
            if hasattr(self, '_context'):
                self._context = TracedTLSContext(self._context, trace)

        def getresponse(self):
            started = time.monotonic()
            trace.emit('http_headers_start', host=self.host)
            try:
                response = super().getresponse()
            except Exception as error:
                trace.emit('http_headers_error', seconds=round(time.monotonic() - started, 3),
                           **error_fields(error))
                raise
            fields = {'status': response.status, 'seconds': round(time.monotonic() - started, 3),
                      'contentLength': response.getheader('Content-Length')}
            location = response.getheader('Location')
            if location:
                fields['location'] = public_url(location)
            trace.emit('http_headers_complete', **fields)
            return response
    return TracedConnection


class TracedHTTPHandler(HTTPHandler):
    def __init__(self, trace):
        super().__init__()
        self.connection = connection_class(http.client.HTTPConnection, trace)

    def http_open(self, request):
        return self.do_open(self.connection, request)


class TracedHTTPSHandler(HTTPSHandler):
    def __init__(self, trace):
        super().__init__()
        self.connection = connection_class(http.client.HTTPSConnection, trace)

    def https_open(self, request):
        return self.do_open(self.connection, request, context=self._context)


class TracedResponse:
    def __init__(self, response, trace):
        self.response = response
        self.trace = trace
        self.received = 0
        self.last_data = time.monotonic()
        self.last_report = self.last_data

    def __getattr__(self, name):
        return getattr(self.response, name)

    def __enter__(self):
        self.response.__enter__()
        return self

    def __exit__(self, kind, value, tb):
        self.trace.emit('response_error' if value else 'response_complete', bytes=self.received,
                        **(error_fields(value) if value else {}))
        return self.response.__exit__(kind, value, tb)

    def read1(self, size=-1):
        if self.received == 0:
            self.trace.emit('body_start')
        try:
            data = self.response.read1(size)
        except Exception as error:
            self.trace.emit('body_error', bytes=self.received,
                            idleSeconds=round(time.monotonic() - self.last_data, 3), **error_fields(error))
            raise
        now = time.monotonic()
        if data:
            self.last_data = now
            first = self.received == 0
            self.received += len(data)
            if first or now - self.last_report >= 10:
                self.trace.emit('body_first_data' if first else 'body_progress', bytes=self.received)
                self.last_report = now
        else:
            self.trace.emit('body_eof', bytes=self.received)
        return data
