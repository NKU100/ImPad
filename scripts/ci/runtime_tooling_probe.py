"""Exercise managed ReDroid launch and relay cancellation."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.ci.adb_relay import DeviceRelay, normalize_adb_server_socket


def _device_serials(output):
    return [line.split()[0] for line in output.splitlines()[1:] if line.split()]


def _read_launch_steps(path):
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def probe(root, app_id='wechat'):
    from scripts.ci.redroid_environment_probe import SERIAL

    root = root.resolve()
    task = root / 'task'
    task.mkdir(parents=True, exist_ok=True)
    workspace = root / 'checkout'
    workspace.mkdir(exist_ok=True)
    adb = os.environ.get('IMPAD_ADB_BINARY', 'adb')
    inherited = os.environ.get('ADB_SERVER_SOCKET')
    endpoint = normalize_adb_server_socket(inherited) if inherited else normalize_adb_server_socket(
        str(root / 'private-adb.sock'))
    owns_server = inherited is None
    env = dict(os.environ, ADB_SERVER_SOCKET=endpoint)
    state = {
        'appId': app_id,
        'worktree': str(workspace),
        'runtime': {'socket': endpoint, 'serverOwned': owns_server, 'serial': SERIAL},
    }
    (task / 'state.json').write_text(json.dumps(state, indent=2) + '\n')
    relay = None
    probe_failed = False
    report = {
        'schemaVersion': 1,
        'status': 'FAILED',
        'endpoint': endpoint,
        'serverOwned': owns_server,
        'failedStep': None,
        'stepLog': 'app-launch-steps.jsonl',
    }
    current_step = 'start-private-adb-server' if owns_server else 'verify-inherited-adb-server'
    try:
        if owns_server:
            subprocess.run([adb, '-L', endpoint, 'start-server'], env=env,
                           check=True, timeout=30, capture_output=True)
        if not owns_server:
            devices = subprocess.check_output([adb, 'devices'], env=env, text=True, timeout=30)
            if _device_serials(devices) != [SERIAL]:
                raise RuntimeError('Tooling probe requires one dedicated ReDroid on the selected ADB endpoint')
        subprocess.run([adb, 'connect', SERIAL], env=env, check=True,
                       timeout=30, capture_output=True)
        if owns_server:
            devices = subprocess.check_output([adb, 'devices'], env=env, text=True, timeout=30)
            if _device_serials(devices) != [SERIAL]:
                raise RuntimeError('Tooling probe requires one dedicated ReDroid on the private ADB endpoint')
        current_step = 'wait-for-device'
        subprocess.run([adb, '-s', SERIAL, 'wait-for-device'], env=env, check=True,
                       timeout=120, capture_output=True)

        relay = DeviceRelay(task, env)
        env['IMPAD_ADB_RELAY'] = str(relay.root)
        wrapper = str(Path(__file__).with_name('adb-device.sh').resolve())
        current_step = 'app-launch'
        launch = subprocess.run(['bash', wrapper, 'app-launch'], cwd=workspace, env=env,
                                capture_output=True, timeout=300, check=False)
        if launch.returncode:
            rows = _read_launch_steps(task / 'app-launch-steps.jsonl')
            failed = next((row for row in reversed(rows) if row.get('status') != 'PASSED'), None)
            failed_label = failed.get('label') if failed else 'unknown-step'
            failed_status = failed.get('status') if failed else f'exit-{launch.returncode}'
            report.update(failedStep=failed_label, failedStatus=failed_status,
                          stepExitCode=failed.get('exitCode') if failed else launch.returncode)
            detail = f'app-launch failed at {failed_label} ({failed_status})'
            if failed and failed.get('stderr'):
                detail += ': ' + failed['stderr'].strip()
            raise RuntimeError(detail)

        current_step = 'binary-screenshot'
        screenshot = subprocess.check_output(
            ['bash', wrapper, 'exec-out', 'screencap', '-p'], cwd=workspace, env=env, timeout=45)
        if not screenshot.startswith(b'\x89PNG\r\n\x1a\n'):
            raise RuntimeError('Relay screenshot is not a PNG')

        current_step = 'cancelled-request-queue-recovery'
        client = subprocess.Popen(['bash', wrapper, 'shell', 'sleep', '30'], cwd=workspace, env=env)
        try:
            time.sleep(1)
            client.terminate()
            client.wait(timeout=5)
            started = time.monotonic()
            state_result = subprocess.check_output(
                ['bash', wrapper, 'get-state'], cwd=workspace, env=env, timeout=5)
            if state_result.strip() != b'device':
                raise RuntimeError('ADB relay queue did not return to the device state')
            seconds = time.monotonic() - started
        finally:
            if client.poll() is None:
                client.kill()
                client.wait()
        report.update(status='PASSED', managedLaunch=True, binaryScreenshot=True,
                      queueRecoveredSeconds=seconds)
        print('Managed ReDroid launch, binary screenshot and cancelled-request queue recovery passed')
        return True
    except Exception as error:
        probe_failed = True
        report.update(status='FAILED', failedStep=report.get('failedStep') or current_step,
                      error=f'{type(error).__name__}: {error}')
        raise RuntimeError(f'{current_step} failed: {error}') from error
    finally:
        cleanup_error = None
        if relay is not None:
            try:
                relay.close()
            except Exception as error:
                cleanup_error = f'ADB relay shutdown failed: {type(error).__name__}: {error}'
        step_log = task / 'app-launch-steps.jsonl'
        try:
            if step_log.is_file():
                shutil.copyfile(step_log, root / 'app-launch-steps.jsonl')
        except OSError as error:
            cleanup_error = cleanup_error or f'Could not retain app-launch step log: {error}'
        report['retainedStepLog'] = (root / 'app-launch-steps.jsonl').is_file()
        if owns_server:
            try:
                stopped = subprocess.run([adb, '-L', endpoint, 'kill-server'], env=env,
                                         check=False, timeout=30, capture_output=True)
                report['serverShutdown'] = 'PASSED' if stopped.returncode == 0 else f'FAILED_{stopped.returncode}'
                if stopped.returncode:
                    cleanup_error = cleanup_error or 'Private ADB server did not stop cleanly'
            except Exception as error:
                report['serverShutdown'] = 'FAILED'
                cleanup_error = cleanup_error or f'Private ADB server shutdown failed: {error}'
        else:
            report['serverShutdown'] = 'NOT_OWNED'
        if cleanup_error:
            report['cleanupError'] = cleanup_error
            if not probe_failed:
                report['status'] = 'FAILED'
        (root / 'tooling-report.json').write_text(json.dumps(report, indent=2) + '\n')
        if cleanup_error and not probe_failed:
            raise RuntimeError(cleanup_error)


if __name__ == '__main__':
    probe(Path('work/runtime-smoke/tooling'), os.environ.get('APP_ID', 'wechat'))
