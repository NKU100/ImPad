"""Trusted decoder for no-account QQ SSO request header identities."""
import struct

MAX_FRAME = 262144
MAX_STREAMS = 64
MAX_EVENTS = 2048
CONTRACT = 'QQ_SSO_REQUEST_IDENTITY_V1'
SECURITY_COMMANDS = frozenset({
    'trpc.o3.ecdh_access.EcdhAccess.SsoSecureAccess',
    'trpc.o3.ecdh_access.EcdhAccess.SsoEstablishShareKey',
})


def tea_decrypt_zero_key(data):
    if len(data) < 16 or len(data) % 8:
        raise ValueError('Invalid TEA block length')
    mask = 0xffffffff
    output = bytearray()
    previous_cipher = previous_plain = 0
    for offset in range(0, len(data), 8):
        cipher = int.from_bytes(data[offset:offset + 8], 'big')
        value = previous_plain ^ cipher
        x, y, total = value >> 32, value & mask, 0xe3779b90
        for _ in range(16):
            y = (y - ((x + total) ^ ((x << 4) & mask) ^ (x >> 5))) & mask
            x = (x - ((y + total) ^ ((y << 4) & mask) ^ (y >> 5))) & mask
            total = (total - 0x9e3779b9) & mask
        previous_plain = (x << 32) | y
        output.extend((previous_plain ^ previous_cipher).to_bytes(8, 'big'))
        previous_cipher = cipher
    start = (output[0] & 7) + 3
    if start > len(output) - 7 or output[-7:] != bytes(7):
        raise ValueError('Invalid TEA padding')
    return bytes(output[start:-7])


def length_field(data, position, limit):
    if position < 0 or position + 4 > limit:
        raise ValueError('Missing field length')
    size = struct.unpack_from('>I', data, position)[0]
    if size < 4 or position + size > limit:
        raise ValueError('Invalid field boundary')
    return data[position + 4:position + size], position + size


def decode_request(frame):
    if not 16 <= len(frame) <= MAX_FRAME or int.from_bytes(frame[:4], 'big') != len(frame):
        raise ValueError('Invalid frame length')
    if int.from_bytes(frame[4:8], 'big') != 12 or frame[8] not in (0, 2):
        raise ValueError('Unsupported no-account protocol')
    ticket, position = length_field(frame, 9, len(frame))
    if ticket or position >= len(frame) or frame[position] != 0:
        raise ValueError('Authenticated or malformed request')
    account, position = length_field(frame, position + 1, len(frame))
    if account != b'0':
        raise ValueError('Request is not from the no-account baseline')
    plain = tea_decrypt_zero_key(frame[position:]) if frame[8] == 2 else frame[position:]
    if len(plain) < 36:
        raise ValueError('Truncated SSO header')
    header_length = int.from_bytes(plain[:4], 'big')
    if not 36 <= header_length <= len(plain):
        raise ValueError('Invalid SSO header boundary')
    _, position = length_field(plain, 28, header_length)
    command, position = length_field(plain, position, header_length)
    if not 1 <= len(command) <= 160 or any(byte < 32 or byte >= 127 for byte in command):
        raise ValueError('Invalid command encoding')
    app_id = int.from_bytes(plain[8:12], 'big')
    if not 0 < app_id <= 0x7fffffff:
        raise ValueError('Invalid packet AppID')
    return {'command': command.decode('ascii'), 'packetAppId': app_id,
            'sequence': int.from_bytes(plain[4:8], 'big')}


class RequestStreams:
    """Bounded stream reassembly; do not search arbitrary bytes for frame starts."""

    def __init__(self):
        self.streams = {}
        self.rejected = set()
        self.frames = []
        self.errors = []
        self.unsupported = 0
        self.events = 0

    def feed(self, key, data):
        self.events += 1
        if self.events > MAX_EVENTS or len(data) > MAX_FRAME:
            self.errors.append('Capture limit exceeded')
            return
        if key in self.rejected:
            return
        if key not in self.streams and len(self.streams) + len(self.rejected) >= MAX_STREAMS:
            self.errors.append('Stream limit exceeded')
            return
        buffer = self.streams.setdefault(key, bytearray())
        buffer.extend(data)
        while len(buffer) >= 4:
            size = int.from_bytes(buffer[:4], 'big')
            if not 16 <= size <= MAX_FRAME:
                self.unsupported += 1
                self.rejected.add(key)
                del self.streams[key]
                return
            if len(buffer) < size:
                return
            frame = bytes(buffer[:size])
            del buffer[:size]
            try:
                decoded = decode_request(frame)
            except ValueError:
                self.unsupported += 1
                continue
            if decoded['command'] in SECURITY_COMMANDS:
                self.frames.append(decoded)

    def result(self):
        return {'contract': CONTRACT, 'frames': self.frames,
                'captureHealthy': not self.errors and not any(self.streams.values()), 'errors': list(dict.fromkeys(self.errors)),
                'unsupportedFrames': self.unsupported,
                'partialStreams': sum(bool(value) for value in self.streams.values())}
