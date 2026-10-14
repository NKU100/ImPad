"""Run fixed QQ acceptance outside the model's serialized ADB command relay."""
import json
from pathlib import Path
import signal
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.ci.codex_goal import verify, verification_error


def main():
    directory = Path(sys.argv[1]).resolve()
    state = json.loads((directory / 'state.json').read_text())
    if state.get('appId') != 'qq':
        raise ValueError('Managed request identity smoke is available only for QQ')
    cancelled = False
    def interrupted(number, frame):
        nonlocal cancelled
        if cancelled:
            return
        cancelled = True
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        raise KeyboardInterrupt('Host runtime verification cancelled')
    signal.signal(signal.SIGTERM, interrupted)
    try:
        verify(directory)
        print('Independent static validation and fixed QQ request identity smoke passed.')
        return 0
    except KeyboardInterrupt:
        print('Host runtime verification cancelled', file=sys.stderr)
        return 130
    except Exception as error:
        print(verification_error(directory, error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
