import json
import subprocess
import tempfile
import unittest
import io
import time
import threading
from unittest.mock import patch
from pathlib import Path

from scripts.ci.codex_trace import EncryptedTrace
from scripts.ci.codex_goal import AppServer


class EncryptedTraceTest(unittest.TestCase):
    def test_background_sealing_does_not_hold_event_recording_lock(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            key = directory / 'identity'
            subprocess.run(['age-keygen', '-o', str(key)], check=True, capture_output=True)
            recipient = subprocess.check_output(['age-keygen', '-y', str(key)], text=True).strip()
            trace = EncryptedTrace(directory, recipient, flush_interval=0.02)
            entered, release = threading.Event(), threading.Event()
            original_run = subprocess.run
            def slow_encrypt(*args, **kwargs):
                entered.set()
                if not release.wait(5):
                    raise RuntimeError('Test encryption barrier expired')
                return original_run(*args, **kwargs)
            try:
                with patch('scripts.ci.codex_trace.subprocess.run', side_effect=slow_encrypt):
                    trace.record('response', {'number': 1})
                    self.assertTrue(entered.wait(5))
                    completed = threading.Event()
                    def record():
                        trace.record('response', {'number': 2})
                        completed.set()
                    writer = threading.Thread(target=record)
                    writer.start()
                    try:
                        self.assertTrue(completed.wait(1), 'Recording blocked behind encryption')
                    finally:
                        release.set()
                        writer.join()
            finally:
                release.set()
                trace.close()
            records = subprocess.check_output(['age', '-d', '-i', str(key), str(trace.path)], text=True)
            self.assertEqual([1, 2], [json.loads(line)['message']['number'] for line in records.splitlines()])

    def test_idle_trace_seals_below_threshold_without_closing_stream(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            key = directory / 'identity'
            subprocess.run(['age-keygen', '-o', str(key)], check=True, capture_output=True)
            recipient = subprocess.check_output(['age-keygen', '-y', str(key)], text=True).strip()
            trace = EncryptedTrace(directory, recipient, flush_interval=0.02)
            try:
                trace.record('response', {'method': 'item/started', 'private': 'tool details'})
                deadline = time.monotonic() + 5
                chunks = []
                while not chunks and time.monotonic() < deadline:
                    chunks = list(trace.chunks.glob('*.age'))
                    time.sleep(0.01)
                self.assertEqual(1, len(chunks))
                self.assertIsNone(trace.process.poll())
                records = subprocess.check_output(['age', '-d', '-i', str(key), str(chunks[0])], text=True)
                self.assertEqual('tool details', json.loads(records)['message']['private'])
                self.assertNotIn(b'tool details', chunks[0].read_bytes())
                trace.record('response', {'method': 'item/completed'})
            finally:
                trace.close()
            records = subprocess.check_output(['age', '-d', '-i', str(key), str(trace.path)], text=True)
            self.assertEqual(2, len(records.splitlines()))

    def test_app_server_records_requests_and_successful_tool_events(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            key = directory / 'identity'
            subprocess.run(['age-keygen', '-o', str(key)], check=True, capture_output=True)
            recipient = subprocess.check_output(['age-keygen', '-y', str(key)], text=True).strip()
            client = AppServer.__new__(AppServer)
            client.trace = EncryptedTrace(directory, recipient)
            event = {'method': 'item/completed', 'params': {'item': {'type': 'commandExecution', 'id': 'tool', 'exitCode': 0, 'aggregatedOutput': 'successful sensitive output'}}}
            class Process:
                stdin = io.StringIO()
                stdout = io.StringIO(json.dumps(event) + '\n')
            client.process = Process()
            client.secrets = []
            client.messages = []
            client.tool_output = {}
            client.send({'method': 'turn/start', 'params': {'text': 'full prompt'}})
            client.read()
            client.trace.close()
            records = [json.loads(line) for line in subprocess.check_output(['age', '-d', '-i', str(key), str(directory / 'session.jsonl.age')], text=True).splitlines()]
            self.assertEqual('full prompt', records[0]['message']['params']['text'])
            self.assertEqual(event, records[1]['message'])

    def test_complete_events_round_trip_without_plaintext_file(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            key = directory / 'identity'
            subprocess.run(['age-keygen', '-o', str(key)], check=True, capture_output=True)
            recipient = subprocess.check_output(['age-keygen', '-y', str(key)], text=True).strip()
            trace = EncryptedTrace(directory, recipient)
            trace.record('request', {'method': 'turn/start', 'params': {'text': 'sensitive prompt'}})
            trace.record('response', {'method': 'item/commandExecution/outputDelta', 'params': {'delta': 'successful output' * 1000}})
            trace.close()
            encrypted = directory / 'session.jsonl.age'
            self.assertNotIn(b'sensitive prompt', encrypted.read_bytes())
            self.assertEqual({'identity', 'session.jsonl.age', 'trace-chunks'}, {p.name for p in directory.iterdir()})
            records = [json.loads(line) for line in subprocess.check_output(['age', '-d', '-i', str(key), str(encrypted)], text=True).splitlines()]
            self.assertEqual(['request', 'response'], [record['direction'] for record in records])
            self.assertEqual('sensitive prompt', records[0]['message']['params']['text'])
            self.assertEqual('successful output' * 1000, records[1]['message']['params']['delta'])
            self.assertTrue(all(record['timestamp'] for record in records))
            wrong_key = directory / 'wrong-identity'
            subprocess.run(['age-keygen', '-o', str(wrong_key)], check=True, capture_output=True)
            result = subprocess.run(['age', '-d', '-i', str(wrong_key), str(encrypted)], capture_output=True)
            self.assertNotEqual(0, result.returncode)
            self.assertEqual(b'', result.stdout)

    def test_completed_chunks_survive_interrupted_main_stream(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            key = directory / 'identity'
            subprocess.run(['age-keygen', '-o', str(key)], check=True, capture_output=True)
            recipient = subprocess.check_output(['age-keygen', '-y', str(key)], text=True).strip()
            trace = EncryptedTrace(directory, recipient)
            for number in range(32):
                trace.record('response', {'number': number, 'text': 'private output'})
            deadline = time.monotonic() + 5
            while not list(trace.chunks.glob('*.age')) and time.monotonic() < deadline:
                time.sleep(0.01)
            chunk = next((directory / 'trace-chunks').glob('*.age'))
            self.assertNotIn(b'private output', chunk.read_bytes())
            trace.process.kill()
            trace.process.wait()
            with self.assertRaises(RuntimeError):
                trace.close()
            records = [json.loads(line) for line in subprocess.check_output(
                ['age', '-d', '-i', str(key), str(chunk)], text=True).splitlines()]
            self.assertEqual(list(range(32)), [r['message']['number'] for r in records])
            self.assertFalse((directory / 'session.jsonl.age').exists())

    def test_invalid_recipient_leaves_no_uploadable_file(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            with self.assertRaises(ValueError):
                EncryptedTrace(directory, 'not-a-public-key')
            self.assertFalse(any(directory.rglob('*.age')))

    def test_failed_encryption_removes_partial_artifact(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            key = directory / 'identity'
            subprocess.run(['age-keygen', '-o', str(key)], check=True, capture_output=True)
            recipient = subprocess.check_output(['age-keygen', '-y', str(key)], text=True).strip()
            trace = EncryptedTrace(directory, recipient)
            trace.record('request', {'text': 'never publish plaintext'})
            trace.process.kill()
            trace.process.wait()
            with self.assertRaises(RuntimeError):
                trace.close()
            self.assertFalse((directory / 'session.jsonl.age').exists())
            self.assertFalse((directory / 'session.jsonl.age.partial').exists())
