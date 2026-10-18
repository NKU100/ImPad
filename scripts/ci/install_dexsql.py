"""Install the pinned DexSQL CLI and licenses after verifying every download."""

import argparse
import hashlib
import platform
from pathlib import Path
import shutil
import subprocess
import tempfile

VERSION = 'v0.1.1'
HASHES = {
    'dexsql-linux-arm64': '8095a4acafbfda3c4d502423f185c28063132b2fb3b11489aba7ee2fe2dcd13f',
    'dexsql-linux-amd64': 'f35c57610fcee62864e68f12e51997621375c025447aa2f32a0be9eb2df464bc',
    'dexsql-darwin-arm64': 'd983de33007e11c56e681aaa238ae15637eff961623335f1131527f8d2c82a77',
    'dexsql-darwin-amd64': '54dfbe421a287dfcf23a63cbe1eac3a42d9f73083ea63e299ae3438dccd0fef4',
    'LICENSE': 'a5e8ba355d3bbc08a1de333b93cfaadff62c1b335454930c3bc8163d710b5a30',
    'LICENSE-AUXILIARY': '91c80095437ed20c2dca7758f63f03e15eedb46aa4854577b5df20c788ecc514',
}


def download(url, destination):
    subprocess.run(['curl', '--fail', '--silent', '--show-error', '--location',
                    '--retry', '3', '--connect-timeout', '30', '--max-time', '300',
                    '--output', str(destination), url], check=True)


def install(destination, system=None, machine=None):
    system = (system or platform.system()).lower()
    machine = (machine or platform.machine()).lower()
    architecture = {'arm64': 'arm64', 'aarch64': 'arm64',
                    'x86_64': 'amd64', 'amd64': 'amd64'}.get(machine)
    asset = f'dexsql-{system}-{architecture}'
    if asset not in HASHES:
        raise ValueError(f'Unsupported DexSQL platform: {system}/{machine}')
    destination = Path(destination).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.dexsql-', dir=destination.parent) as temporary:
        staging = Path(temporary)
        for name in (asset, 'LICENSE', 'LICENSE-AUXILIARY'):
            path = staging / name
            download(f'https://github.com/LSPosed/dexsql-release/releases/download/{VERSION}/{name}', path)
            if hashlib.sha256(path.read_bytes()).hexdigest() != HASHES[name]:
                raise ValueError(f'DexSQL SHA-256 mismatch: {name}')
        (destination / 'bin').mkdir(parents=True, exist_ok=True)
        binary = destination / 'bin/dexsql'
        (staging / asset).chmod(0o755)
        for name in ('LICENSE', 'LICENSE-AUXILIARY'):
            shutil.copyfile(staging / name, destination / name)
        (staging / asset).replace(binary)
    return binary


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    options = parser.parse_args()
    binary = install(options.output)
    subprocess.run([str(binary), '--help'], check=True, timeout=30)
    print(f'Installed DexSQL {VERSION}: {binary}')
