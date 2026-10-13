"""Observe native socket sends; never call QQ methods or persist payloads."""
import argparse
import json
import threading
import time
from pathlib import Path
from scripts.ci.apps.qq.request_protocol import RequestStreams


def main():
    import frida
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, required=True)
    parser.add_argument('--pid', type=int, required=True)
    parser.add_argument('--agent', type=Path, required=True)
    parser.add_argument('--ready', type=Path, required=True)
    parser.add_argument('--seconds', type=int, default=30)
    args = parser.parse_args()
    if not 1 <= args.seconds <= 45:
        raise ValueError('Invalid capture duration')
    streams = RequestStreams()
    ready = threading.Event()
    lock = threading.Lock()
    device = frida.get_device_manager().add_remote_device(f'127.0.0.1:{args.port}')
    session = device.attach(args.pid)
    script = None

    def message(value, data):
        with lock:
            if value.get('type') != 'send':
                streams.errors.append('Observer script failed')
                return
            event = value.get('payload', {})
            if event.get('kind') == 'ready' and event.get('pid') == args.pid:
                ready.set()
            elif event.get('kind') == 'bytes' and data:
                streams.feed((event['fd'], event['generation']), data)
            elif event.get('kind') == 'capture-error':
                streams.errors.append('Native capture failed')

    def detached(reason, crash=None):
        with lock:
            streams.errors.append('Target process detached: ' + str(reason))

    session.on('detached', detached)
    try:
        script = session.create_script(args.agent.read_text())
        script.on('message', message)
        script.load()
        if not ready.wait(10):
            raise RuntimeError('Native observer did not become ready')
        args.ready.write_text('ready\n')
        time.sleep(args.seconds)
    finally:
        session.off('detached', detached)
        if script is not None:
            try:
                script.unload()
            except frida.InvalidOperationError:
                streams.errors.append('Observer script was destroyed before cleanup')
        try:
            session.detach()
        except frida.InvalidOperationError:
            streams.errors.append('Observer session was detached before cleanup')
    with lock:
        result = streams.result()
    print(json.dumps(result))


if __name__ == '__main__':
    main()
