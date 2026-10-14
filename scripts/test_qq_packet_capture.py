import unittest
from scripts.ci.apps.qq.packet_capture import TcpCapture
from scripts.test_qq_requests import packet


def event(seq, payload=b'', flags=0, **extra):
    return dict(kind='segment', pid=123, uid=10232, started=44, inode=55,
                source='0a000211', destination='01020304', sourcePort=1234,
                destinationPort=8080, sequence=seq, flags=flags, generation=100, payload=payload.hex(), **extra)


class PacketCaptureTest(unittest.TestCase):
    def capture(self, segments, drops=0):
        c=TcpCapture(10232)
        c.feed(dict(kind='ready',uid=10232))
        for e in segments:c.feed(e)
        c.feed(dict(kind='end',kernelDrops=drops))
        return c.result()

    def test_reassembles_out_of_order_and_matching_retransmission(self):
        p=packet()
        r=self.capture([event(100,flags=2),event(111,p[10:]),event(101,p[:10]),event(101,p[:10])])
        self.assertTrue(r['captureHealthy']);self.assertEqual(r['frames'][0]['packetAppId'],10)

    def test_rejects_contradictory_retransmission(self):
        p=packet()
        self.assertFalse(self.capture([event(100,flags=2),event(101,p),event(101,b'x')])['captureHealthy'])

    def test_missing_syn_gap_partial_and_kernel_drop_fail_closed(self):
        p=packet()
        for segments,drops in [([event(101,p)],0),([event(100,flags=2),event(102,p)],0),
                               ([event(100,flags=2),event(101,p[:-1])],0),
                               ([event(100,flags=2),event(101,p)],1)]:
            with self.subTest(segments=segments,drops=drops):
                self.assertFalse(self.capture(segments,drops)['captureHealthy'])

    def test_rejects_wrong_uid_and_bad_process_identity(self):
        for key,value in [('uid',123),('pid',0),('started',False),('inode',0)]:
            e=event(100,flags=2);e[key]=value
            self.assertFalse(self.capture([e])['captureHealthy'])

    def test_requires_ready_and_end_and_limits(self):
        c=TcpCapture(10232);self.assertFalse(c.result()['captureHealthy'])
        c.feed({'kind':'ready','uid':10232})
        c.feed(event(100,flags=2));c.feed(event(101,b'x'*65536))
        c.feed({'kind':'end','kernelDrops':0});self.assertFalse(c.result()['captureHealthy'])
