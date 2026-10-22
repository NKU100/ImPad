"""Execute sandbox device requests without granting the model network access."""
import json
import importlib
import os
from pathlib import Path
import re
import subprocess
import signal
import shlex
import shutil
import sys
import threading
import time
from datetime import datetime, timezone
from scripts.ci.app_policy import get_policy

COMMANDS = {'app-launch', 'wechat-launch', 'verify-build', 'verify-runtime', 'shell', 'exec-out', 'push', 'pull', 'install', 'uninstall', 'logcat',
            'reboot', 'root', 'unroot', 'wait-for-device', 'get-state', 'version', 'help'}


def normalize_adb_server_socket(value):
    if not isinstance(value, str) or not value or any(char.isspace() for char in value):
        raise ValueError('ADB server endpoint is missing or malformed')
    if value.startswith('tcp:'):
        address = value.removeprefix('tcp:')
        match = re.fullmatch(r'(?:\[[0-9a-fA-F:.%]+\]|[^:\[\]]+):(\d+)', address)
        if not match or not 1 <= int(match[1]) <= 65535:
            raise ValueError('ADB TCP endpoint is malformed')
        return value
    if value.startswith('localfilesystem:'):
        if not value.removeprefix('localfilesystem:'):
            raise ValueError('ADB filesystem endpoint has an empty path')
        return value
    if ':' in value:
        raise ValueError('ADB server endpoint uses an unsupported scheme')
    path = Path(value).expanduser()
    return 'localfilesystem:' + (str(path) if path.is_absolute() else str(path.resolve()))


def validate_request(request, workspace):
    args = request['args']
    if (not isinstance(args, list) or not 1 <= len(args) <= 64
            or any(not isinstance(a, str) or len(a) > 8192 or '\0' in a for a in args)
            or args[0] not in COMMANDS):
        raise ValueError('Use a device command; the ReDroid and ADB server are fixed by the controller.')
    cwd = Path(request['cwd']).resolve()
    if not cwd.is_relative_to(workspace):
        raise ValueError('Device command must run inside the adaptation workspace')
    if args[0] in {'app-launch', 'wechat-launch', 'verify-build', 'verify-runtime'} and len(args) != 1:
        raise ValueError('Managed launch/build commands do not accept arguments')
    def local_path(value):
        if not (cwd / value).resolve().is_relative_to(workspace):
            raise ValueError('ADB local files must stay inside the adaptation workspace')
    if args[0] == 'install':
        files = [a for a in args[1:] if not a.startswith('-')]
        if len(files) != 1 or any(a.startswith('-') and a not in {'-r', '-g', '-t', '-d', '--no-streaming', '--streaming'} for a in args[1:]):
            raise ValueError('Use install with one workspace APK and supported flags')
        local_path(files[0])
    elif args[0] == 'push':
        files = [a for a in args[1:] if not a.startswith('-')]
        if len(files) < 2 or any(a.startswith('-') for a in args[1:]):
            raise ValueError('Use push with workspace files and one device destination')
        for value in files[:-1]:
            local_path(value)
    elif args[0] == 'pull':
        if len(args) not in {2, 3} or any(a.startswith('-') for a in args[1:]):
            raise ValueError('Use pull with one device source and optional workspace destination')
        local_path(args[2] if len(args) == 3 else '.')
    return args, cwd


class DeviceRelay:
    def __init__(self, directory, env):
        state = json.loads((directory / 'state.json').read_text())
        self.policy = get_policy(state.get('appId'))
        self.directory = directory.resolve()
        self.workspace = Path(state['worktree']).resolve()
        self.root = self.workspace / 'work/analysis/adb-relay'
        self.root.mkdir(parents=True, exist_ok=True)
        self.fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        runtime = state['runtime']
        self.adb = env.get('IMPAD_ADB_BINARY') or shutil.which('adb')
        if not self.adb:
            raise RuntimeError('Native host adb is unavailable')
        self.serial = runtime['serial']
        self.env = dict(env, ADB_SERVER_SOCKET=normalize_adb_server_socket(runtime['socket']))
        self.stop = threading.Event()
        self.process_lock = threading.Lock()
        self.process = None
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def write(self, name, data):
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600, dir_fd=self.fd)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)

    def execute(self, command, cwd, request, timeout, *, cleanup_grace=3):
        with self.process_lock:
            if self.stop.is_set():
                raise InterruptedError('Runtime relay stopped')
            self.process = subprocess.Popen(command, cwd=cwd, env=self.env,
                                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                            start_new_session=True)
        deadline = time.monotonic() + timeout
        try:
            while True:
                try:
                    os.stat(request, dir_fd=self.fd, follow_symlinks=False)
                    cancelled = self.stop.is_set()
                except FileNotFoundError:
                    cancelled = True
                if cancelled or time.monotonic() >= deadline:
                    try:
                        os.killpg(self.process.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    try:
                        stdout, stderr = self.process.communicate(timeout=cleanup_grace)
                    except subprocess.TimeoutExpired:
                        os.killpg(self.process.pid, signal.SIGKILL)
                        stdout, stderr = self.process.communicate()
                    reason = 'Managed request cancelled' if cancelled else f'Managed command exceeded {timeout} seconds'
                    return (130 if cancelled else 124), stdout, stderr + ('\n' + reason + '\n').encode()
                try:
                    stdout, stderr = self.process.communicate(timeout=0.1)
                    return self.process.returncode, stdout, stderr
                except subprocess.TimeoutExpired:
                    pass
        finally:
            with self.process_lock:
                self.process = None

    def dispatch(self, args, cwd, request):
        if args[0] == 'verify-runtime':
            if self.policy.app_id != 'qq':
                raise ValueError('Managed request identity smoke is available only for QQ')
            return self.execute([sys.executable, str(Path(__file__).with_name('host_runtime.py')),
                                 str(self.directory)], self.workspace, request, 900, cleanup_grace=60)
        if args[0] == 'verify-build':
            return self.execute([sys.executable, str(Path(__file__).with_name('host_build.py')),
                                 str(self.directory)], self.workspace, request, 900)
        prefix = [self.adb, '-s', self.serial]
        if args[0] in {'app-launch', 'wechat-launch'}:
            policy = self.policy if args[0] == 'app-launch' else get_policy('wechat')
            from scripts.ci.runtime_smoke import MAGISK, PAGE_SIZE_BACKCOMPAT_COMMAND
            strategy = importlib.import_module(policy.runtime_ui_strategy)
            component = getattr(strategy, 'launcher_component', policy.package_name + '/.ui.LauncherUI')
            commands = [
                ('wait-for-device', prefix + ['wait-for-device']),
                ('page-size-compatibility', prefix + ['shell', shlex.join(
                    [MAGISK, 'su', '-c', PAGE_SIZE_BACKCOMPAT_COMMAND])]),
                ('force-stop', prefix + ['shell', 'am', 'force-stop', policy.package_name]),
                ('launch-activity', prefix + ['shell', 'am', 'start', '-W', '-n', component]),
            ]
            log_path = self.directory / f'{args[0]}-steps.jsonl'
            fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            os.close(fd)
            for label, command in commands:
                started_at = datetime.now(timezone.utc).isoformat()
                started = time.monotonic()
                result = self.execute(command, self.workspace, request, 60)
                finished = time.monotonic()
                finished_at = datetime.now(timezone.utc).isoformat()
                code, stdout, stderr = result
                status = ('PASSED' if code == 0 else 'TIMED_OUT' if code == 124
                          else 'CANCELLED' if code == 130 else 'FAILED')
                row = {
                    'label': label,
                    'startedAt': started_at,
                    'finishedAt': finished_at,
                    'durationMs': max(0, round((finished - started) * 1000)),
                    'status': status,
                    'exitCode': code,
                    'stdout': stdout.decode(errors='replace'),
                    'stderr': stderr.decode(errors='replace'),
                }
                log_fd = os.open(log_path, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW)
                with os.fdopen(log_fd, 'ab') as stream:
                    stream.write((json.dumps(row, sort_keys=True) + '\n').encode())
                if result[0]:
                    return result
            return result
        timeout = 240 if args[0] == 'install' else 120 if args[0] in {'push', 'pull', 'wait-for-device'} else 30
        if args[0] == 'install':
            args = [args[0], *(arg if arg.startswith('-') else str((cwd / arg).resolve())
                               for arg in args[1:])]
        elif args[0] == 'push':
            args = [args[0], *(str((cwd / arg).resolve()) for arg in args[1:-1]), args[-1]]
        elif args[0] == 'pull':
            destination = str(cwd if len(args) == 2 else (cwd / args[2]).resolve())
            if len(args) == 3 and args[2].endswith('/') and not destination.endswith('/'):
                destination += '/'
            args = [args[0], args[1], destination]
        return self.execute(prefix + args, self.workspace, request, timeout)

    def serve(self):
        while not self.stop.is_set():
            for name in os.listdir(self.fd):
                if not re.fullmatch(r'[a-f0-9]{32}\.request', name):
                    continue
                key = name[:-8]
                try:
                    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=self.fd)
                    with os.fdopen(fd, 'rb') as stream:
                        request = json.loads(stream.read(65536))
                    args, cwd = validate_request(request, self.workspace)
                    code, stdout, stderr = self.dispatch(args, cwd, name)
                except FileNotFoundError:
                    continue
                except Exception as error:
                    code, stdout, stderr = 2, b'', (str(error) + '\n').encode()
                try:
                    self.write(key + '.stdout', stdout)
                    self.write(key + '.stderr', stderr)
                    os.unlink(name, dir_fd=self.fd)
                    self.write(key + '.result', json.dumps({'exitCode': code}).encode())
                except OSError:
                    pass
            self.stop.wait(0.05)

    def close(self):
        self.stop.set()
        with self.process_lock:
            if self.process is not None:
                try:
                    os.killpg(self.process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        self.thread.join(timeout=5)
        os.close(self.fd)
