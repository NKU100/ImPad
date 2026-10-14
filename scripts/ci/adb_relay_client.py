"""Call the dedicated device through filesystem IPC in a network-disabled sandbox."""
import json
import os
from pathlib import Path
import sys
import signal
import time
import uuid


def request_timeout(arguments):
    if arguments == ['verify-runtime']:
        return 1080
    return 960 if arguments == ['verify-build'] else 300


def main():
    def interrupted(number, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupted)
    root = Path(os.environ['IMPAD_ADB_RELAY'])
    key = uuid.uuid4().hex
    temporary = root / (key + '.tmp')
    try:
        temporary.write_text(json.dumps({'args': sys.argv[1:], 'cwd': os.getcwd()}))
        temporary.replace(root / (key + '.request'))
        deadline = time.monotonic() + request_timeout(sys.argv[1:])
        while time.monotonic() < deadline:
            result = root / (key + '.result')
            if result.exists():
                try:
                    report = json.loads(result.read_text())
                except json.JSONDecodeError:
                    time.sleep(0.05)
                    continue
                sys.stdout.buffer.write((root / (key + '.stdout')).read_bytes())
                sys.stderr.buffer.write((root / (key + '.stderr')).read_bytes())
                return report['exitCode']
            time.sleep(0.05)
        print('Dedicated ADB relay timed out', file=sys.stderr)
        return 124
    except KeyboardInterrupt:
        return 130
    finally:
        for suffix in ['tmp', 'request', 'result', 'stdout', 'stderr']:
            (root / (key + '.' + suffix)).unlink(missing_ok=True)


if __name__ == '__main__':
    sys.exit(main())
