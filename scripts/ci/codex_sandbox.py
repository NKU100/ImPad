"""Expose only clean adaptation inputs and tool dependencies to the model server."""

import json
import shutil
import sys
from pathlib import Path


def prepare_qq_java(tools, env):
    real_home = (Path(env['IMPAD_REAL_JAVA']).parent.parent if env.get('IMPAD_REAL_JAVA') else Path(env['JAVA_HOME'])).resolve()
    def link(source, target):
        if target.is_symlink() and target.resolve() == source.resolve():
            return
        target.symlink_to(source, target_is_directory=source.is_dir())
    shadow = tools / 'java-home'
    (shadow / 'bin').mkdir(parents=True, exist_ok=True)
    for entry in real_home.iterdir():
        if entry.name != 'bin':
            link(entry, shadow / entry.name)
    for entry in (real_home / 'bin').iterdir():
        if entry.name != 'java':
            link(entry, shadow / 'bin' / entry.name)
    launcher = tools / 'bin' / 'java'
    launcher.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(Path(__file__).with_name('java-analysis.py'), launcher)
    launcher.chmod(0o755)
    link(launcher, shadow / 'bin' / 'java')
    env['IMPAD_REAL_JAVA'] = str(real_home / 'bin' / 'java')
    env['IMPAD_JAVA_LOCK'] = str(Path(env['IMPAD_JADX_LOCK']).with_name('java-analysis.lock'))
    env['JAVA_HOME'] = str(shadow)


def model_command(directory, env, command):
    if sys.platform != 'linux':
        return command
    workspace = directory / 'checkout'
    home = workspace / 'work/analysis/model-home'
    home.mkdir(parents=True, exist_ok=True)
    debug_key = Path(env.get('WECHATPAD_DEBUG_KEYSTORE', str(Path(env['HOME']) / '.android/debug.keystore')))
    if debug_key.is_file():
        (home / '.android').mkdir(exist_ok=True)
        shutil.copyfile(debug_key, home / '.android/debug.keystore')
        if env.get('WECHATPAD_DEBUG_KEYSTORE'):
            env['WECHATPAD_DEBUG_KEYSTORE'] = str(home / '.android/debug.keystore')
    gradle = home / '.gradle'
    cached = Path(env.get('GRADLE_USER_HOME', str(Path(env['HOME']) / '.gradle')))
    for name in ['caches/modules-2', 'wrapper/dists']:
        source = cached / name
        if source.is_dir():
            shutil.copytree(source, gradle / name, dirs_exist_ok=True)
    env['HOME'] = str(home)
    env['JAVA_TOOL_OPTIONS'] = f'-Duser.home="{home}"'
    env['GRADLE_USER_HOME'] = str(gradle)
    env['TMPDIR'] = '/tmp'
    env['IMPAD_JADX_LOCK'] = str(workspace / 'work/analysis/jadx.lock')
    args = ['bwrap', '--die-with-parent', '--new-session', '--unshare-pid',
            '--proc', '/proc', '--dev', '/dev', '--tmpfs', '/tmp']
    mounted = []
    for name in ['/usr', '/bin', '/sbin', '/lib', '/lib64', '/etc', '/opt']:
        if Path(name).exists():
            args += ['--ro-bind', name, name]
            mounted.append(Path(name).resolve())
    if Path('/run/systemd/resolve').is_dir():
        args += ['--ro-bind', '/run/systemd/resolve', '/run/systemd/resolve']
    def bind(path, writable=False):
        path = Path(path).resolve()
        if any(path.is_relative_to(root) for root in mounted):
            return
        args.extend(['--bind' if writable else '--ro-bind', str(path), str(path)])
        mounted.append(path)
    bind(workspace, True)
    bind(home, True)
    bind(env['CODEX_HOME'], True)
    for name in ['JAVA_HOME', 'ANDROID_HOME', 'ANDROID_SDK_ROOT']:
        if env.get(name):
            bind(env[name])
    jadx = shutil.which('jadx', path=env['PATH'])
    if jadx:
        bind(Path(jadx).resolve().parent.parent)
    state = json.loads((directory / 'state.json').read_text())
    if state.get('runtime'):
        tools = directory / 'device-tools'
        (tools / 'bin').mkdir(parents=True, exist_ok=True)
        wrapper = tools / 'bin/adb'
        shutil.copyfile(Path(__file__).with_name('adb-device.sh'), wrapper)
        shutil.copyfile(Path(__file__).with_name('adb_relay_client.py'), tools / 'bin/adb_relay_client.py')
        wrapper.chmod(0o755)
        builder = tools / 'bin/impad-build'
        shutil.copyfile(Path(__file__).with_name('host-build.sh'), builder)
        builder.chmod(0o755)
        if env.get('APP_ID') == 'qq':
            prepare_qq_java(tools, env)
            smoke = tools / 'bin/impad-smoke'
            shutil.copyfile(Path(__file__).with_name('host-runtime.sh'), smoke)
            smoke.chmod(0o755)
        bind(tools)
        env['PATH'] = str(tools / 'bin') + ':' + env['PATH']
        env['IMPAD_ADB_RELAY'] = str(workspace / 'work/analysis/adb-relay')
    args += ['--chdir', str(workspace), '--', *command]
    return args
