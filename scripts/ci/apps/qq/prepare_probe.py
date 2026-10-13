"""Prepare pinned QQ observation tools in the trusted controller's runtime directory."""
import argparse
import hashlib
import lzma
from pathlib import Path
import shutil
import subprocess
from urllib.request import urlopen
import venv

VERSION = '17.23.1'
SERVER_DIGESTS = {
    'arm64': '3f490e9812dae7f050c0132f2b734612e823cc273e92bcc40874082f01b92f1f',
    'x86_64': '612a1036adee065aa4434511c4024b4b0e0c815c7b8f63b4997b288a10669ade',
}


def prepare(root, abi):
    tool = root / 'qq-probe'
    tool.mkdir(parents=True, exist_ok=True)
    archive = tool / 'server.xz'
    expected = SERVER_DIGESTS[abi]
    if not archive.is_file() or hashlib.sha256(archive.read_bytes()).hexdigest() != expected:
        url = f'https://github.com/frida/frida/releases/download/{VERSION}/frida-server-{VERSION}-android-{abi}.xz'
        with urlopen(url, timeout=120) as response:
            body = response.read(60_000_001)
        if len(body) > 60_000_000 or hashlib.sha256(body).hexdigest() != expected:
            raise ValueError('Frida server archive does not match its pinned SHA-256')
        archive.write_bytes(body)
    (tool / 'frida-server').write_bytes(lzma.decompress(archive.read_bytes()))
    venv.EnvBuilder(with_pip=True).create(tool / 'venv')
    subprocess.run([str(tool / 'venv/bin/python'), '-m', 'pip', 'install', f'frida=={VERSION}'], check=True)
    shutil.copyfile(Path(__file__).parent / 'probe/agent.js', tool / 'agent.js')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--abi', choices=SERVER_DIGESTS, required=True)
    args = parser.parse_args()
    prepare(args.root.resolve(), args.abi)


if __name__ == '__main__':
    main()
