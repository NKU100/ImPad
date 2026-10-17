#!/usr/bin/env bash
set -euo pipefail

# Google SDK native host tools are x86_64; Android remains native ARM64.
[[ $(uname -m) == aarch64 ]]
sudo dpkg --add-architecture amd64
sudo python3 - <<'PY'
from pathlib import Path
source = Path('/etc/apt/sources.list.d/ubuntu.sources')
text = source.read_text()
paragraphs = []
for paragraph in text.strip().split('\n\n'):
    lines = [line for line in paragraph.splitlines() if not line.startswith('Architectures:')]
    if any(line.startswith('Types:') for line in lines):
        lines.append('Architectures: arm64')
    paragraphs.append('\n'.join(lines))
source.write_text('\n\n'.join(paragraphs) + '\n')
Path('/etc/apt/sources.list.d/impad-sdk-amd64.sources').write_text('''Types: deb
URIs: http://archive.ubuntu.com/ubuntu/
Suites: noble noble-updates
Components: main universe
Architectures: amd64
Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg

Types: deb
URIs: http://security.ubuntu.com/ubuntu/
Suites: noble-security
Components: main universe
Architectures: amd64
Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg
''')
PY
sudo apt-get update -qq
sudo apt-get install -y --no-install-recommends qemu-user-static binfmt-support libc6:amd64 libstdc++6:amd64 zlib1g:amd64
sudo update-binfmts --enable qemu-x86_64
