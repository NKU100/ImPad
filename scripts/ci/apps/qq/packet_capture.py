"""Bounded controller-side reassembly of kernel-attributed outgoing TCP segments."""
import hashlib
from pathlib import Path
import re
from scripts.ci.apps.qq.request_protocol import RequestStreams, MAX_FRAME, CONTRACT

CAPTURE_KIND = 'KERNEL_SOCKET_REQUEST_IDENTITY_V1'
MAX_EVENTS = 4096
MAX_BYTES = 8 * 1024 * 1024


def observer_source_digest():
    root = Path(__file__).parent
    digest = hashlib.sha256()
    for name in ('probe/collector.c', 'packet_capture.py', 'request_protocol.py', 'prepare_probe.py'):
        digest.update(name.encode() + b'\0' + root.joinpath(name).read_bytes() + b'\0')
    return digest.hexdigest()


class TcpCapture:
    def __init__(self, uid):
        self.uid = uid
        self.flows = {}
        self.errors = []
        self.ready = self.ended = False
        self.events = self.total = 0
        self.kernel_drops = None

    def feed(self, event):
        try:
            self._feed(event)
        except (ValueError, KeyError, TypeError):
            self.errors.append('Invalid kernel capture event')

    def _feed(self, event):
        if not isinstance(event, dict) or self.ended:
            raise ValueError()
        kind = event.get('kind')
        if kind == 'ready':
            if self.ready or type(event.get('uid')) is not int or event['uid'] != self.uid:
                raise ValueError()
            self.ready = True
            return
        if not self.ready:
            raise ValueError()
        if kind == 'end':
            drops = event.get('kernelDrops')
            if type(drops) is not int or drops < 0:
                raise ValueError()
            self.kernel_drops = drops
            self.ended = True
            if drops:
                self.errors.append('Kernel capture dropped packets')
            return
        if kind != 'segment':
            raise ValueError()
        self.events += 1
        if self.events > MAX_EVENTS:
            raise ValueError()
        for field in ('pid', 'uid', 'started', 'inode'):
            if type(event.get(field)) is not int or not 0 < event[field] <= 0xffffffffffffffff:
                raise ValueError()
        if event['uid'] != self.uid or event['pid'] > 0x7fffffff:
            raise ValueError()
        for field, maximum in [('sourcePort',65535),('destinationPort',65535),('sequence',0xffffffff),('flags',255),('generation',0xffffffff)]:
            if type(event.get(field)) is not int or not 0 <= event[field] <= maximum:
                raise ValueError()
        for field in ('source','destination'):
            if not isinstance(event.get(field),str) or not re.fullmatch(r'(?:[a-f0-9]{8}|[a-f0-9]{32})',event[field]):
                raise ValueError()
        if len(event['source']) != len(event['destination']):
            raise ValueError()
        data = event.get('payload')
        if not isinstance(data,str) or len(data)>131070 or len(data)%2 or not re.fullmatch('[a-f0-9]*',data):
            raise ValueError()
        data=bytes.fromhex(data)
        self.total += len(data)
        if self.total > MAX_BYTES:
            raise ValueError()
        key=tuple(event[f] for f in ('pid','uid','started','inode','source','destination','sourcePort','destinationPort','generation'))
        if key not in self.flows:
            if len(self.flows)>=64:
                raise ValueError()
            self.flows[key]={'syn':None,'segments':[]}
        flow=self.flows[key]
        seq=event['sequence']
        if event['flags']&2:
            syn=(seq+1)&0xffffffff
            if flow['syn'] not in (None,syn):
                raise ValueError()
            flow['syn']=syn
            seq=syn
        if data:
            flow['segments'].append((seq,data))

    def result(self):
        errors=list(self.errors)
        if not self.ready or not self.ended:
            errors.append('Kernel capture did not finish')
        frames=[];owners=set();partial=0
        for owner,flow in self.flows.items():
            if not flow['segments']:
                continue
            if flow['syn'] is None:
                errors.append('Outgoing TCP stream has no captured SYN')
                continue
            segments=sorted((((seq-flow['syn'])&0xffffffff,data) for seq,data in flow['segments']),key=lambda value:value[0])
            buffer=bytearray()
            for offset,data in segments:
                if offset>len(buffer):
                    errors.append('Outgoing TCP stream has a gap')
                    break
                overlap=min(len(data),len(buffer)-offset)
                if buffer[offset:offset+overlap]!=data[:overlap]:
                    errors.append('Contradictory TCP retransmission')
                    break
                buffer.extend(data[overlap:])
                if len(buffer)>MAX_BYTES:
                    errors.append('TCP stream exceeds capture limit')
                    break
            else:
                decoder=RequestStreams()
                # Keep individual feeds bounded while preserving complete stream framing.
                for pos in range(0,len(buffer),MAX_FRAME):
                    decoder.feed(owner,bytes(buffer[pos:pos+MAX_FRAME]))
                result=decoder.result()
                errors.extend(result['errors']);partial+=result['partialStreams']
                if result['partialStreams']:
                    errors.append('Incomplete outgoing request frame')
                if result['frames']:
                    frames.extend(result['frames']);owners.add(owner[:4])
        return dict(contract=CONTRACT,captureKind=CAPTURE_KIND,frames=frames,
                    captureHealthy=not errors,errors=list(dict.fromkeys(errors)),
                    kernelDrops=self.kernel_drops,partialStreams=partial,
                    ownershipVerified=bool(owners),owners=[dict(pid=p,uid=u,started=s,inode=i) for p,u,s,i in sorted(owners)])
