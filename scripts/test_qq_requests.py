import copy
import hashlib
from pathlib import Path
import struct
import unittest
from scripts.ci.apps.qq.request_protocol import (
    CONTRACT, SECURITY_COMMANDS, MAX_FRAME, RequestStreams, decode_request, tea_decrypt_zero_key)
from scripts.ci.apps.qq.runtime_policy import build_request_identity_proof, validate_request_evidence


def encrypt(payload):
    padding = (8 - (len(payload) + 10) % 8) % 8
    plain = bytes([padding]) + bytes(padding + 2) + payload + bytes(7)
    previous_cipher = previous_plain = 0
    output = b''
    mask = 0xffffffff
    for offset in range(0, len(plain), 8):
        intermediate = int.from_bytes(plain[offset:offset + 8], 'big') ^ previous_cipher
        x, y, total = intermediate >> 32, intermediate & mask, 0
        for _ in range(16):
            total = (total + 0x9e3779b9) & mask
            x = (x + ((y + total) ^ ((y << 4) & mask) ^ (y >> 5))) & mask
            y = (y + ((x + total) ^ ((x << 4) & mask) ^ (x >> 5))) & mask
        previous_cipher = ((x << 32) | y) ^ previous_plain
        previous_plain = intermediate
        output += previous_cipher.to_bytes(8, 'big')
    return output


def field(value):
    return struct.pack('>I', len(value) + 4) + value


def packet(appid=10, command=None, account=b'0', flag=2):
    command = (command or sorted(SECURITY_COMMANDS)[0]).encode()
    header = struct.pack('>III', 1, appid, 2052) + bytes(12) + field(b'') + field(command)
    plain = field(header) + field(b'')
    outer = struct.pack('>IB', 12, flag) + field(b'') + b'\0' + field(account)
    return field(outer + (encrypt(plain) if flag == 2 else plain))


def proof(apk='a'*64, module='b'*64):
    from scripts.ci.apps.qq.packet_capture import observer_source_digest, CAPTURE_KIND
    observer = observer_source_digest()
    base = dict(contract=CONTRACT, captureHealthy=True, errors=[], apkSha256=apk,
                moduleApkSha256=module, observerSha256=observer,
                processName='com.tencent.mobileqq:MSF', scopeVerified=True, captureKind=CAPTURE_KIND,
                ndkVersion='29.0.14206865', ownershipVerified=True, kernelDrops=0, partialStreams=0, appUid=10232, collectorBinarySha256='c'*64,
                owners=[dict(pid=123,uid=10232,started=44,inode=55)])
    cmd = sorted(SECURITY_COMMANDS)[0]
    return build_request_identity_proof(
        expectation(apk),
        dict(base, moduleEnabled=False,
             frames=[dict(command=cmd, packetAppId=10, sequence=1, firstObservedMonotonicNs=100)]),
        dict(base, moduleEnabled=True,
             frames=[dict(command=cmd, packetAppId=20, sequence=2, firstObservedMonotonicNs=200)]),
    )


def expectation(apk='a'*64):
    return dict(contract=CONTRACT, apkSha256=apk, phoneAppId=10, tabletAppId=20)


class QqRequestTest(unittest.TestCase):
    def test_shared_proof_builder_binds_exact_expectation_and_capture_schema(self):
        request = expectation()
        evidence = proof()
        self.assertEqual({'contract', 'apkSha256', 'phoneAppId', 'tabletAppId', 'disabled', 'enabled'},
                         set(evidence))
        for invalid_expectation in (dict(request, unexpected=True),
                                    {key: value for key, value in request.items() if key != 'apkSha256'}):
            with self.assertRaises(ValueError):
                build_request_identity_proof(
                    invalid_expectation, evidence['disabled'], evidence['enabled'])
        changed_capture = copy.deepcopy(evidence['enabled'])
        changed_capture['unexpected'] = True
        with self.assertRaises(ValueError):
            build_request_identity_proof(request, evidence['disabled'], changed_capture)
        changed_capture = copy.deepcopy(evidence['enabled'])
        changed_capture['apkSha256'] = 'c' * 64
        with self.assertRaises(ValueError):
            build_request_identity_proof(request, evidence['disabled'], changed_capture)

    def test_tea_roundtrip_and_padding(self):
        for size in range(1, 50):
            value = bytes(range(size))
            self.assertEqual(value, tea_decrypt_zero_key(encrypt(value)))
        with self.assertRaises(ValueError):
            tea_decrypt_zero_key(bytes(15))
        with self.assertRaises(ValueError):
            tea_decrypt_zero_key(bytes(16))

    def test_decode_only_no_account_frames(self):
        self.assertEqual(10, decode_request(packet())['packetAppId'])
        self.assertEqual(20, decode_request(packet(20, flag=0))['packetAppId'])
        for data in [packet(account=b'123'), packet(flag=1), bytes(10), packet()[:-1]]:
            with self.assertRaises(ValueError):
                decode_request(data)

    def test_reassembly_fragmented_and_multiple_packets(self):
        streams = RequestStreams()
        data = packet() + packet(20)
        for byte in data:
            streams.feed((1, 0), bytes([byte]))
        self.assertEqual([10, 20], [f['packetAppId'] for f in streams.result()['frames']])
        streams.feed((1, 1), packet(30))
        self.assertEqual(30, streams.result()['frames'][-1]['packetAppId'])

    def test_partial_frames_cannot_accept(self):
        streams = RequestStreams()
        streams.feed((1, 0), packet()[:-1])
        self.assertFalse(streams.result()['captureHealthy'])
        self.assertEqual([], streams.result()['frames'])

    def test_unsupported_and_limits_cannot_forge_a_result(self):
        streams = RequestStreams()
        streams.feed((1, 0), b'\xff'*20)
        streams.feed((1, 0), packet())
        self.assertEqual([], streams.result()['frames'])
        streams.feed((2, 0), bytes(MAX_FRAME + 1))
        self.assertFalse(streams.result()['captureHealthy'])
        other = RequestStreams()
        other.feed((1, 0), packet(command='CliLogSvc.UploadReq'))
        self.assertEqual([], other.result()['frames'])

    def test_positive_and_rejected_acceptance(self):
        good = proof()
        self.assertTrue(validate_request_evidence(good, 'a'*64, 'b'*64, expectation()))
        self.assertFalse(validate_request_evidence(good, 'a'*64, 'b'*64, dict(expectation(), tabletAppId=30)))
        self.assertFalse(validate_request_evidence(good, 'a'*64, 'b'*64, dict(expectation(), apkSha256='c'*64)))
        self.assertFalse(validate_request_evidence(good, 'a'*64, 'b'*64, dict(expectation(), phoneAppId=10.0)))
        for key, value in [('apkSha256', 'c' * 64), ('unexpected', True)]:
            bad = copy.deepcopy(good); bad[key] = value
            self.assertFalse(validate_request_evidence(bad, 'a' * 64, 'b' * 64, expectation()))
        bad = copy.deepcopy(good); del bad['apkSha256']
        self.assertFalse(validate_request_evidence(bad, 'a' * 64, 'b' * 64, expectation()))
        cases = [('packetAppId', 10), ('packetAppId', True), ('command', 'CliLogSvc.UploadReq'), ('sequence', -1)]
        for key, value in cases:
            bad = copy.deepcopy(good);bad['enabled']['frames'][0][key] = value
            self.assertFalse(validate_request_evidence(bad, 'a'*64, 'b'*64, expectation()))
        for key, value in [('scopeVerified', False), ('moduleEnabled', False), ('captureHealthy', False),
                           ('apkSha256', 'c'*64), ('observerSha256', 'c'*64), ('moduleApkSha256', 'c'*64)]:
            bad = copy.deepcopy(good);bad['enabled'][key] = value
            self.assertFalse(validate_request_evidence(bad, 'a'*64, 'b'*64, expectation()))
        bad = copy.deepcopy(good);bad['enabled']['frames'].append(dict(command=sorted(SECURITY_COMMANDS)[1], packetAppId=10, sequence=3))
        self.assertFalse(validate_request_evidence(bad, 'a'*64, 'b'*64, expectation()))



    def test_kernel_ownership_and_capture_contract_are_required(self):
        for key,value in [('captureKind','FRIDA'),('ownershipVerified',False),('kernelDrops',1),
                          ('kernelDrops',False),('partialStreams',1),('appUid',False),('owners',[]),
                          ('collectorBinarySha256','bad')]:
            p=proof();p['enabled'][key]=value
            self.assertFalse(validate_request_evidence(p,'a'*64,'b'*64,expectation()))
        p=proof();p['enabled']['owners'][0]['uid']=123
        self.assertFalse(validate_request_evidence(p,'a'*64,'b'*64,expectation()))
        for key, value in [('unknownEvidence', True)]:
            p = proof(); p['enabled'][key] = value
            self.assertFalse(validate_request_evidence(p, 'a' * 64, 'b' * 64, expectation()))
        p = proof(); p['enabled']['frames'][0]['unexpected'] = True
        self.assertFalse(validate_request_evidence(p, 'a' * 64, 'b' * 64, expectation()))
        p = proof(); p['enabled']['owners'][0]['unexpected'] = True
        self.assertFalse(validate_request_evidence(p, 'a' * 64, 'b' * 64, expectation()))

if __name__ == '__main__':
    unittest.main()
