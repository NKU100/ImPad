"""Run guarded host validation requested by the isolated adaptation agent."""
import json
import os
from pathlib import Path
import signal
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.ci.codex_adaptation import validate
from scripts.ci.codex_goal import auth_secrets, redact, verification_error


def main():
    directory = Path(sys.argv[1]).resolve()
    state = json.loads((directory / 'state.json').read_text())
    workspace = Path(state['worktree']).resolve()
    output = workspace / 'work/analysis/host-build'
    if not output.resolve().is_relative_to(workspace):
        raise ValueError('Host build logs must stay inside the adaptation workspace')
    output.mkdir(parents=True, exist_ok=True)
    def interrupted(number, frame):
        raise InterruptedError('Host build cancelled')
    signal.signal(signal.SIGTERM, interrupted)
    try:
        validate(directory)
        print('Independent static checks, fixed acceptance regression, debug and release app unit tests, compatibility module tests, metadata validation and debug APK build passed. Install app/build/outputs/apk/debug/app-debug.apk for runtime validation. Independent runtime acceptance is still required.')
        return 0
    except Exception as error:
        print(verification_error(directory, error), file=sys.stderr)
        return 1
    finally:
        secrets = auth_secrets()
        fd = os.open(output, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for log in [directory / 'build.log', *directory.glob('check-*.log')]:
                if log.is_file():
                    destination = os.open(log.name, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600, dir_fd=fd)
                    with os.fdopen(destination, 'w') as stream:
                        stream.write(redact(log.read_text(errors='replace'), secrets))
        finally:
            os.close(fd)


if __name__ == '__main__':
    sys.exit(main())
