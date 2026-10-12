"""Encrypt credential-free native Codex state for trusted CI continuation."""
import io
import subprocess
import sqlite3
import tarfile
import tempfile
import re
from pathlib import Path, PurePosixPath

MAX_BYTES = 256 * 1024 * 1024


def permitted(name):
    path = PurePosixPath(name)
    return (not path.is_absolute() and '..' not in path.parts
            and (path.parts[0] in {'sessions', 'archived_sessions'}
                 or (len(path.parts) == 1 and path.name.startswith(('state_', 'goals_', 'thread_history_'))
                     and '.sqlite' in path.name)))


def session_files(home):
    home = Path(home).resolve()
    for path in sorted(home.rglob('*')):
        relative = path.relative_to(home).as_posix()
        if (path.is_file() and permitted(relative)
                and not any(p.is_symlink() for p in [path, *path.parents])):
            yield path


def validate_identity(identity, recipient):
    if not re.fullmatch(r'age1[0-9a-z]{58}', recipient):
        raise ValueError('CODEX_CHECKPOINT_RECIPIENT must be an age X25519 public key')
    with tempfile.NamedTemporaryFile(mode='w', prefix='impad-age-') as key:
        key.write(identity); key.flush()
        result = subprocess.run(['age-keygen', '-y', key.name], capture_output=True,
                                text=True, check=True, timeout=10)
    if result.stdout.strip() != recipient:
        raise ValueError('Checkpoint age identity does not match CODEX_CHECKPOINT_RECIPIENT')


def save_session(directory, home, recipient, identity):
    directory, home = Path(directory), Path(home).resolve()
    validate_identity(identity, recipient)
    if not (directory / 'native-thread.json').is_file():
        return False
    content = io.BytesIO()
    with tarfile.open(fileobj=content, mode='w:gz') as archive:
        for path in session_files(home):
            archive.add(path, arcname=path.relative_to(home).as_posix(), recursive=False)
    target = directory / 'native-session.tar.age'
    partial = target.with_suffix('.partial')
    try:
        with partial.open('wb') as output:
            subprocess.run(['age', '--encrypt', '-r', recipient], input=content.getvalue(),
                           stdout=output, stderr=subprocess.PIPE, check=True, timeout=60)
        partial.replace(target)
    finally:
        partial.unlink(missing_ok=True)
    return True


def restore_session(directory, home, identity, recipient):
    directory, home = Path(directory), Path(home).resolve()
    if not identity.strip():
        raise ValueError('CODEX_CHECKPOINT_IDENTITY is required to resume the native thread')
    validate_identity(identity, recipient)
    with tempfile.NamedTemporaryFile(mode='w', prefix='impad-age-') as key:
        key.write(identity); key.flush()
        result = subprocess.run(['age', '--decrypt', '-i', key.name,
                                 str(directory / 'native-session.tar.age')],
                                capture_output=True, check=True, timeout=60)
    with tarfile.open(fileobj=io.BytesIO(result.stdout), mode='r:gz') as archive:
        members = archive.getmembers()
        if sum(m.size for m in members) > MAX_BYTES:
            raise ValueError('Native checkpoint exceeds the extraction limit')
        if any(not m.isfile() or not permitted(m.name) for m in members):
            raise ValueError('Native checkpoint contains an unsafe path')
        home.mkdir(mode=0o700, parents=True, exist_ok=True)
        for member in members:
            target = home / member.name
            if any(p.is_symlink() for p in [target, *target.parents]):
                raise ValueError('Native checkpoint destination contains a symbolic link')
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            target.write_bytes(archive.extractfile(member).read())
            target.chmod(0o600)

    # Codex stores absolute rollout paths. A new runner/home must point at the
    # restored files, while the original thread identity remains unchanged.
    for database in home.glob('state_*.sqlite'):
        with sqlite3.connect(database) as connection:
            if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='threads'").fetchone():
                continue
            for thread_id, old_path in connection.execute('SELECT id, rollout_path FROM threads').fetchall():
                parts = PurePosixPath(old_path).parts
                start = next((i for i, part in enumerate(parts) if part in {'sessions', 'archived_sessions'}), None)
                if start is None:
                    raise ValueError('Native thread rollout path is outside the session directories')
                relative = PurePosixPath(*parts[start:])
                if not permitted(str(relative)) or not (home / relative).is_file():
                    raise ValueError('Native thread rollout is missing from the checkpoint')
                connection.execute('UPDATE threads SET rollout_path=? WHERE id=?',
                                   (str(home / relative), thread_id))
