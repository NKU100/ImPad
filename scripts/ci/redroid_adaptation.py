"""Own a disposable ReDroid during the managed adaptation goal."""
import argparse
import json
import os
from pathlib import Path
import subprocess
from scripts.ci.redroid_environment_probe import Probe, SERIAL
from scripts.ci.runtime_adaptation import initialize
from scripts.ci.runtime_tooling_probe import probe


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--controller', type=Path, required=True)
    args = parser.parse_args()
    framework = Probe(args.root.resolve() / 'framework')
    try:
        framework.setup()
        initialize(args.directory.resolve(), args.root.resolve(), SERIAL)
        state = json.loads((args.directory / 'state.json').read_text())
        # Exercise the same relay using the controller-owned device socket.
        os.environ['ADB_SERVER_SOCKET'] = 'localfilesystem:' + state['runtime']['socket']
        probe(args.root.resolve() / 'tooling', state['appId'])
        subprocess.run(['node', str(args.controller / 'scripts/ci/codex_live_cli.mjs')], check=True)
    finally:
        framework.cleanup()


if __name__ == '__main__':
    main()
