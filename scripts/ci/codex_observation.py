"""Capture live Codex state for observation without interrupting its turn."""

import argparse
import json
import os
import re
import sqlite3
import subprocess
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from scripts.ci.codex_session_checkpoint import MAX_BYTES, session_files


def capture_observation(directory, home, destination, recipient):
    directory, home, destination = Path(directory), Path(home), Path(destination)
    if not re.fullmatch(r'age1[0-9a-z]{58}', recipient):
        raise ValueError('Observation requires a valid trace encryption recipient')
    thread = directory / 'native-thread.json'
    if not thread.is_file():
        return False
    manifest = {'kind': 'codex-session-observation', 'resumable': False,
                'startedAt': datetime.now(timezone.utc).isoformat(),
                'thread': json.loads(thread.read_text()), 'files': []}
    partial = destination.with_suffix('.partial')
    try:
        with tempfile.TemporaryDirectory(prefix='impad-observation-') as temporary:
            staged = Path(temporary) / 'files'
            staged.mkdir(mode=0o700)
            total = 0
            for source in session_files(home):
                relative = source.relative_to(home.resolve())
                target = staged / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                original_size = source.stat().st_size
                if original_size + total > MAX_BYTES:
                    raise ValueError('Observation exceeds the capture limit')
                omitted = 0
                if source.suffix == '.sqlite':
                    db = sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)
                    backup = sqlite3.connect(target)
                    try:
                        db.backup(backup, pages=256, sleep=0.01)
                    finally:
                        backup.close()
                        db.close()
                else:
                    # The rollout may be growing. Capture a bounded prefix and omit
                    # its unfinished last record rather than chasing the writer.
                    with source.open('rb') as content, target.open('wb') as output:
                        remaining = original_size
                        while remaining:
                            block = content.read(min(remaining, 1024 * 1024))
                            if not block:
                                break
                            output.write(block)
                            remaining -= len(block)
                    if source.suffix == '.jsonl':
                        with target.open('rb+') as output:
                            data = output.read()
                            end = data.rfind(b'\n') + 1
                            omitted = len(data) - end
                            output.truncate(end)
                total += target.stat().st_size
                if total > MAX_BYTES:
                    raise ValueError('Observation exceeds the capture limit')
                manifest['files'].append({'path': relative.as_posix(),
                                          'bytes': target.stat().st_size,
                                          'omittedTailBytes': omitted})
            stderr = directory / 'app-server.stderr.log'
            if stderr.is_file():
                with stderr.open('rb') as content:
                    content.seek(max(0, stderr.stat().st_size - 256 * 1024))
                    (staged / 'app-server.stderr.log').write_bytes(content.read(256 * 1024))
            manifest['completedAt'] = datetime.now(timezone.utc).isoformat()
            (staged / 'observation.json').write_text(json.dumps(manifest, indent=2) + '\n')
            archive_path = Path(temporary) / 'observation.tar.gz'
            with tarfile.open(archive_path, 'w:gz') as archive:
                for source in sorted(staged.rglob('*')):
                    if source.is_file():
                        archive.add(source, arcname=source.relative_to(staged).as_posix(), recursive=False)
            with archive_path.open('rb') as content, partial.open('wb') as output:
                subprocess.run(['age', '-e', '-r', recipient], stdin=content, stdout=output,
                               stderr=subprocess.PIPE, check=True, timeout=60)
            partial.chmod(0o600)
            partial.replace(destination)
    finally:
        partial.unlink(missing_ok=True)
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--home', type=Path, required=True)
    parser.add_argument('--destination', type=Path, required=True)
    args = parser.parse_args()
    saved = capture_observation(args.directory, args.home, args.destination,
                                os.environ.get('CODEX_TRACE_RECIPIENT', ''))
    print(json.dumps({'saved': saved}))


if __name__ == '__main__':
    main()
