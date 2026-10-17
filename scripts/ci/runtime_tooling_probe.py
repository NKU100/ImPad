"""Exercise managed ReDroid launch and relay cancellation."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.ci.adb_relay import DeviceRelay


def probe(root, app_id='wechat'):
    from scripts.ci.redroid_environment_probe import SERIAL
    root = root.resolve()
    task = root / 'task'; task.mkdir(parents=True, exist_ok=True)
    workspace = root / 'checkout'; workspace.mkdir(exist_ok=True)
    existing_socket = os.environ.get('ADB_SERVER_SOCKET')
    socket = Path(existing_socket.removeprefix('localfilesystem:')) if existing_socket else root / 'adb.sock'
    devices = subprocess.check_output([os.environ.get('IMPAD_ADB_BINARY', 'adb'),'devices'],text=True).splitlines()[1:]
    assert [line.split()[0] for line in devices if line.strip()]==[SERIAL], 'Tooling probe requires one dedicated ReDroid'
    if not existing_socket:
        subprocess.run([os.environ.get('IMPAD_ADB_BINARY', 'adb'),'kill-server'],check=True)
        subprocess.run([os.environ.get('IMPAD_ADB_BINARY', 'adb'),'-L','localfilesystem:'+str(socket),'start-server'],check=True)
    env = dict(os.environ, ADB_SERVER_SOCKET='localfilesystem:'+str(socket))
    subprocess.run([os.environ.get('IMPAD_ADB_BINARY', 'adb'),'connect',SERIAL],env=env,check=True,timeout=30)
    subprocess.run([os.environ.get('IMPAD_ADB_BINARY', 'adb'),'-s',SERIAL,'wait-for-device'],env=env,check=True,timeout=120)
    (task/'state.json').write_text(json.dumps({'appId':app_id,'worktree':str(workspace),'runtime':{'socket':str(socket),'serial':SERIAL}}))
    relay = DeviceRelay(task,env)
    env['IMPAD_ADB_RELAY'] = str(relay.root)
    wrapper = str(Path('scripts/ci/adb-device.sh').resolve())
    try:
        subprocess.run(['bash',wrapper,'app-launch'],cwd=workspace,env=env,check=True,timeout=180)
        screenshot = subprocess.check_output(['bash',wrapper,'exec-out','screencap','-p'],cwd=workspace,env=env,timeout=45)
        assert screenshot.startswith(b'\x89PNG\r\n\x1a\n'), 'Relay screenshot is not a PNG'
        client = subprocess.Popen(['bash',wrapper,'shell','sleep','30'],cwd=workspace,env=env)
        try:
            time.sleep(1)
            client.terminate(); client.wait(timeout=5)
            started = time.monotonic()
            state = subprocess.check_output(['bash',wrapper,'get-state'],cwd=workspace,env=env,timeout=5)
            assert state.strip()==b'device'
            seconds = time.monotonic()-started
        finally:
            if client.poll() is None: client.kill(); client.wait()
        (root/'tooling-report.json').write_text(json.dumps({'status':'PASSED','managedLaunch':True,'binaryScreenshot':True,'queueRecoveredSeconds':seconds},indent=2)+'\n')
        print('Managed ReDroid launch, binary screenshot and cancelled-request queue recovery passed')
    finally:
        relay.close()
        if not existing_socket:
            subprocess.run([os.environ.get('IMPAD_ADB_BINARY', 'adb'), 'kill-server'], env=env, check=False)
            subprocess.run([os.environ.get('IMPAD_ADB_BINARY', 'adb'), 'connect', SERIAL], check=True, timeout=30)


if __name__=='__main__':
    probe(Path('work/runtime-smoke/tooling'), os.environ.get('APP_ID', 'wechat'))
