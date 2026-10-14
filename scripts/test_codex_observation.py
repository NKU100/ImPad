import io
import json
import sqlite3
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

from scripts.ci.codex_observation import capture_observation


class CodexObservationTest(unittest.TestCase):
    def test_live_snapshot_excludes_credentials_and_preserves_complete_records_and_wal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            home, task = root / 'home', root / 'task'
            home.mkdir(); task.mkdir()
            (home / 'sessions').mkdir()
            rollout = home / 'sessions/current.jsonl'
            rollout.write_bytes(b'{"type":"session_meta"}\n{"partial":')
            (home / 'auth.json').write_text('private credential')
            (home / 'sessions/linked.jsonl').symlink_to(home / 'auth.json')
            (task / 'native-thread.json').write_text('{"threadId":"current"}')
            (task / 'app-server.stderr.log').write_text('server diagnostic')
            key = root / 'identity'
            subprocess.run(['age-keygen', '-o', str(key)], check=True, capture_output=True)
            recipient = subprocess.check_output(['age-keygen', '-y', str(key)], text=True).strip()
            with sqlite3.connect(home / 'goals_1.sqlite') as db:
                db.execute('PRAGMA journal_mode=WAL')
                db.execute('CREATE TABLE goals(status TEXT)')
                db.execute("INSERT INTO goals VALUES ('active')")
                db.commit()
                output = root / 'session-observation.tar.age'
                self.assertTrue(capture_observation(task, home, output, recipient))
                data = subprocess.check_output(['age', '-d', '-i', str(key), str(output)])
                with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as archive:
                    self.assertNotIn('auth.json', archive.getnames())
                    self.assertNotIn('sessions/linked.jsonl', archive.getnames())
                    self.assertEqual(b'{"type":"session_meta"}\n',
                                     archive.extractfile('sessions/current.jsonl').read())
                    manifest = json.load(archive.extractfile('observation.json'))
                    self.assertFalse(manifest['resumable'])
                    self.assertEqual('current', manifest['thread']['threadId'])
                    self.assertTrue(any(f['omittedTailBytes'] for f in manifest['files']))
                    self.assertEqual(b'server diagnostic', archive.extractfile('app-server.stderr.log').read())
                    snapshot = root / 'snapshot.sqlite'
                    snapshot.write_bytes(archive.extractfile('goals_1.sqlite').read())
                with sqlite3.connect(snapshot) as backup:
                    self.assertEqual('active', backup.execute('SELECT status FROM goals').fetchone()[0])
                    self.assertEqual('ok', backup.execute('PRAGMA integrity_check').fetchone()[0])
                self.assertEqual(b'{"type":"session_meta"}\n{"partial":', rollout.read_bytes())
                self.assertFalse((task / 'native-session.tar.age').exists())
                self.assertNotIn(b'private credential', output.read_bytes())

