"""Compile the trusted external collector for the Android guest's native ABI."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
from scripts.ci.apps.qq.packet_capture import observer_source_digest

NDK_VERSION = '29.0.14206865'
TARGETS = {'arm64':'aarch64-linux-android29', 'x86_64':'x86_64-linux-android29'}


def prepare(root, abi):
    if abi not in TARGETS:
        raise ValueError('Unsupported collector ABI')
    sdk=Path(os.environ.get('ANDROID_HOME') or os.environ.get('ANDROID_SDK_ROOT',''))
    ndk=sdk/'ndk'/NDK_VERSION
    if not ndk.is_dir():
        subprocess.run(['sdkmanager','ndk;'+NDK_VERSION],check=True,timeout=600)
    host={'Darwin':'darwin-x86_64','Linux':'linux-x86_64'}.get(platform.system())
    if host is None:
        raise ValueError('Unsupported collector build host')
    compiler=ndk/'toolchains/llvm/prebuilt'/host/'bin'/(TARGETS[abi]+'-clang')
    tool=Path(root)/'qq-probe';tool.mkdir(parents=True,exist_ok=True)
    binary=tool/'collector'
    subprocess.run([str(compiler),'-O2','-Wall','-Wextra','-Werror','-fPIE','-pie',
                    '-Wl,-z,max-page-size=16384',str(Path(__file__).parent/'probe/collector.c'),'-o',str(binary)],check=True,timeout=120)
    (tool/'collector.json').write_text(json.dumps(dict(abi=abi,ndkVersion=NDK_VERSION,
                    observerSha256=observer_source_digest(),binarySha256=hashlib.sha256(binary.read_bytes()).hexdigest()),indent=2)+'\n')


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--abi',choices=TARGETS,required=True);args=parser.parse_args()
    prepare(args.root.resolve(),args.abi)


if __name__=='__main__':main()
