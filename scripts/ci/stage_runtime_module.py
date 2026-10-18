"""Stage an exact downloaded APK at the runtime harness's existing module path."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil


def stage_module(directory):
    apks = sorted(directory.glob('*.apk'))
    if len(apks) != 1:
        raise ValueError('Runtime module artifact must contain exactly one APK')
    source = apks[0]
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    metadata = directory / 'build.json'
    if metadata.is_file():
        recorded = json.loads(metadata.read_text())
        if recorded['sha256'].get(source.name) != digest:
            raise ValueError('Module APK does not match the build artifact SHA-256')
    destination = directory / 'app-debug.apk'
    if source != destination:
        shutil.copyfile(source, destination)
        source.unlink()
    print(f'Runtime module SHA-256: {digest}')
    return digest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--directory', type=Path, required=True)
    args = parser.parse_args()
    stage_module(args.directory)


if __name__ == '__main__':
    main()
