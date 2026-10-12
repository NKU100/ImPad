import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

class SessionCheckpointTest(unittest.TestCase):
    def test_encrypted_native_state_roundtrip_excludes_auth(self):
        from scripts.ci.codex_session_checkpoint import save_session, restore_session
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); home=root/'home';home.mkdir()
            (home/'sessions').mkdir();(home/'sessions/session.jsonl').write_text('native state')
            (home/'auth.json').write_text('secret authentication')
            directory=root/'checkpoint';directory.mkdir()
            (directory/'native-thread.json').write_text(json.dumps({'threadId':'thread-1'}))
            identity=root/'identity';subprocess.run(['age-keygen','-o',str(identity)],capture_output=True,check=True)
            public=subprocess.check_output(['age-keygen','-y',str(identity)]).decode().strip()
            save_session(directory,home,public,identity.read_text())
            restored=root/'restored'
            restore_session(directory,restored,identity.read_text(),public)
            self.assertEqual('native state',(restored/'sessions/session.jsonl').read_text())
            self.assertFalse((restored/'auth.json').exists())
            self.assertNotIn(b'secret authentication',(directory/'native-session.tar.age').read_bytes())

    def test_database_rollout_paths_relocate_and_goal_state_is_preserved(self):
        import sqlite3
        from scripts.ci.codex_session_checkpoint import save_session, restore_session
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();home=root/'home';home.mkdir()
            (home/'sessions').mkdir();rollout=home/'sessions/thread.jsonl';rollout.write_text('state')
            with sqlite3.connect(home/'state_5.sqlite') as db:
                db.execute('CREATE TABLE threads (id TEXT, rollout_path TEXT)')
                db.execute('INSERT INTO threads VALUES (?,?)',('thread-1',str(rollout)))
            (home/'goals_1.sqlite').write_bytes(b'goal-state')
            checkpoint=root/'checkpoint';checkpoint.mkdir()
            (checkpoint/'native-thread.json').write_text(json.dumps({'threadId':'thread-1'}))
            identity=root/'identity';subprocess.run(['age-keygen','-o',str(identity)],capture_output=True,check=True)
            public=subprocess.check_output(['age-keygen','-y',str(identity)]).decode().strip()
            save_session(checkpoint,home,public,identity.read_text())
            restored=root/'restored';restore_session(checkpoint,restored,identity.read_text(),public)
            with sqlite3.connect(restored/'state_5.sqlite') as db:
                self.assertEqual(str(restored/'sessions/thread.jsonl'),db.execute('SELECT rollout_path FROM threads').fetchone()[0])
            self.assertEqual(b'goal-state',(restored/'goals_1.sqlite').read_bytes())

    def test_symlinks_are_not_archived(self):
        from scripts.ci.codex_session_checkpoint import session_files
        with tempfile.TemporaryDirectory() as tmp:
            home=Path(tmp);(home/'sessions').mkdir();(home/'sessions/link').symlink_to('/etc/passwd')
            self.assertEqual([],list(session_files(home)))
