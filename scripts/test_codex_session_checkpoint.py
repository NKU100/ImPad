import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

class SessionCheckpointTest(unittest.TestCase):
    def test_save_cli_reads_checkpoint_identity_from_environment(self):
        from scripts.ci.codex_session_checkpoint import main
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            directory, home = root / 'checkpoint', root / 'home'
            output = io.StringIO()
            env = {'CODEX_CHECKPOINT_RECIPIENT': 'age1' + 'a' * 58,
                   'CODEX_CHECKPOINT_IDENTITY': 'private identity'}
            argv = ['codex_session_checkpoint', 'save', '--directory', str(directory), '--home', str(home)]
            with patch.dict(os.environ, env), patch.object(sys, 'argv', argv), \
                    patch('scripts.ci.codex_session_checkpoint.save_session', return_value=True) as save, \
                    redirect_stdout(output):
                main()
            self.assertEqual({'saved': True}, json.loads(output.getvalue()))
            save.assert_called_once_with(directory, home, env['CODEX_CHECKPOINT_RECIPIENT'],
                                         env['CODEX_CHECKPOINT_IDENTITY'])

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
            with sqlite3.connect(home/'goals_1.sqlite') as db:
                db.execute('CREATE TABLE goals (status TEXT)')
                db.execute('INSERT INTO goals VALUES (?)',('paused',))
            checkpoint=root/'checkpoint';checkpoint.mkdir()
            (checkpoint/'native-thread.json').write_text(json.dumps({'threadId':'thread-1'}))
            identity=root/'identity';subprocess.run(['age-keygen','-o',str(identity)],capture_output=True,check=True)
            public=subprocess.check_output(['age-keygen','-y',str(identity)]).decode().strip()
            save_session(checkpoint,home,public,identity.read_text())
            restored=root/'restored';restore_session(checkpoint,restored,identity.read_text(),public)
            with sqlite3.connect(restored/'state_5.sqlite') as db:
                self.assertEqual(str(restored/'sessions/thread.jsonl'),db.execute('SELECT rollout_path FROM threads').fetchone()[0])
            with sqlite3.connect(restored/'goals_1.sqlite') as db:
                self.assertEqual('paused', db.execute('SELECT status FROM goals').fetchone()[0])

    def test_live_wal_database_is_captured_as_a_consistent_sqlite_snapshot(self):
        import sqlite3
        from scripts.ci.codex_session_checkpoint import save_session, restore_session
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);home=root/'home';home.mkdir()
            (home/'sessions').mkdir()
            database=home/'state_9.sqlite'
            with sqlite3.connect(database) as db:
                db.execute('PRAGMA journal_mode=WAL')
                db.execute('CREATE TABLE threads (id TEXT, rollout_path TEXT)')
                db.execute('INSERT INTO threads VALUES (?,?)',('thread-9',str(home/'sessions/thread.jsonl')))
            self.assertTrue(database.with_name(database.name+'-wal').exists())
            (home/'sessions/thread.jsonl').write_text('{"event":"turn_started"}\n')
            checkpoint=root/'checkpoint';checkpoint.mkdir()
            (checkpoint/'native-thread.json').write_text(json.dumps({'threadId':'thread-9'}))
            identity=root/'identity';subprocess.run(['age-keygen','-o',str(identity)],capture_output=True,check=True)
            public=subprocess.check_output(['age-keygen','-y',str(identity)]).decode().strip()
            save_session(checkpoint,home,public,identity.read_text())
            restored=root/'restored';restore_session(checkpoint,restored,identity.read_text(),public)
            with sqlite3.connect(restored/'state_9.sqlite') as db:
                self.assertEqual(('thread-9',str((restored/'sessions/thread.jsonl').resolve())),
                                 db.execute('SELECT id, rollout_path FROM threads').fetchone())
                self.assertEqual('ok',db.execute('PRAGMA integrity_check').fetchone()[0])

    def test_symlinks_are_not_archived(self):
        from scripts.ci.codex_session_checkpoint import session_files
        with tempfile.TemporaryDirectory() as tmp:
            home=Path(tmp);(home/'sessions').mkdir();(home/'sessions/link').symlink_to('/etc/passwd')
            self.assertEqual([],list(session_files(home)))
