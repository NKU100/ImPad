"""Continue a durable Goal, publishing checkpoints before starting another turn."""
import base64
import json
import os
import re
import signal
import subprocess
import time
from collections import deque
from pathlib import Path

def redact(text, secrets):
    for secret in sorted(set(secrets), key=len, reverse=True):
        if len(secret) >= 8:
            text = text.replace(secret, '[REDACTED]')
            text = text.replace(base64.b64encode(secret.encode()).decode(), '[REDACTED]')
    text = re.sub(r'eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+', '[REDACTED]', text)
    return re.sub(r'(?i)(bearer\s+|(?:access_token|refresh_token|id_token|api_key)\s*[=:]\s*)[^\s,}]+', r'\1[REDACTED]', text)


def sanitize(value, secrets):
    if isinstance(value, str):
        return redact(value, secrets)
    if isinstance(value, list):
        return [sanitize(v, secrets) for v in value]
    if isinstance(value, dict):
        return {k: sanitize(v, secrets) for k, v in value.items()}
    return value


def read_json(path, fallback):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return fallback


def auth_secrets():
    path = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))) / 'auth.json'
    if not path.is_file():
        return []
    def strings(value):
        if isinstance(value, str):
            return [value]
        if isinstance(value, dict):
            return [s for v in value.values() for s in strings(v)]
        if isinstance(value, list):
            return [s for v in value for s in strings(v)]
        return []
    return strings(json.loads(path.read_text()))


class AppServer:
    def __init__(self, directory, env):
        from scripts.ci.codex_trace import EncryptedTrace
        self.directory = directory
        self.secrets = auth_secrets()
        self.events = deque()
        self.messages = []
        self.tool_output = {}
        self.sequence = 0
        self.trace = EncryptedTrace(directory, os.environ.get('CODEX_TRACE_RECIPIENT', '').strip())
        self.stderr = None
        self.process = None
        self.device_relay = None
        try:
            state = json.loads((directory / 'state.json').read_text())
            if state.get('runtime'):
                from scripts.ci.adb_relay import DeviceRelay
                self.device_relay = DeviceRelay(directory, env)
            self.stderr = (directory / 'app-server.stderr.log').open('w')
            from scripts.ci.codex_sandbox import model_command
            command = model_command(directory, env,
                ['codex', 'app-server', '--listen', 'stdio://', '-c', 'features.goals=true',
                 '-c', 'features.multi_agent=false', '-c', 'features.multi_agent_v2=false',
                 '-c', 'features.apps=false', '-c', 'features.plugins=false',
                 '-c', 'features.shell_snapshot=false', '-c', 'web_search="disabled"'])
            self.process = subprocess.Popen(command,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.stderr,
                text=True, env=env, start_new_session=True)
            self.request('initialize', {'clientInfo': {'name': 'impad_ci', 'version': '1'},
                                        'capabilities': {'experimentalApi': True}})
            self.send({'method': 'initialized'})
        except Exception as error:
            self.close()
            stderr = directory / 'app-server.stderr.log'
            details = redact(stderr.read_text(errors='replace')[-4000:], self.secrets + auth_secrets()) if stderr.is_file() else ''
            raise RuntimeError(str(error) + '\nCodex startup diagnostics:\n' + details) from error

    def send(self, message):
        self.trace.record('request', message)
        self.process.stdin.write(json.dumps(message) + '\n')
        self.process.stdin.flush()

    def read(self):
        line = self.process.stdout.readline()
        if not line:
            raise RuntimeError('Codex app-server exited before completing its request')
        message = json.loads(line)
        self.trace.record('response', message)
        if 'method' in message and 'id' in message:
            self.send({'id': message['id'], 'error': {'code': -32000, 'message': 'Interactive requests are disabled in CI'}})
            return self.read()
        if message.get('method') == 'item/commandExecution/outputDelta':
            params = message.get('params', {})
            item_id = params.get('itemId', '')
            self.tool_output[item_id] = (self.tool_output.get(item_id, '') + params.get('delta', ''))[-2000:]
        if message.get('method') == 'item/completed':
            item = message.get('params', {}).get('item', {})
            if item.get('type') == 'agentMessage':
                self.messages.append(redact(item.get('text', ''), self.secrets + auth_secrets())[:4000])
            elif item.get('type') == 'commandExecution':
                output = item.get('aggregatedOutput') or self.tool_output.pop(item.get('id', ''), '')
                if item.get('exitCode') not in {None, 0}:
                    self.messages.append(redact('Tool failure: ' + output[-2000:], self.secrets + auth_secrets()))
        return message

    def request(self, method, params):
        self.sequence += 1
        request_id = self.sequence
        self.send({'id': request_id, 'method': method, 'params': params})
        while True:
            message = self.read()
            if message.get('id') == request_id:
                if 'error' in message:
                    raise RuntimeError(redact(str(message['error']), self.secrets + auth_secrets()))
                return message.get('result', {})
            if 'method' in message:
                self.events.append(message)

    def wait_turn(self, thread, turn):
        while True:
            message = self.events.popleft() if self.events else self.read()
            params = message.get('params', {})
            if message.get('method') == 'turn/completed' and params.get('threadId') == thread and params['turn']['id'] == turn:
                return params['turn']

    def pause(self, thread):
        self.request('thread/goal/set', {'threadId': thread, 'status': 'paused'})
        # Fence an automatic continuation that raced with the controller's pause.
        state = self.request('thread/read', {'threadId': thread, 'includeTurns': True})
        for turn in state['thread'].get('turns', []):
            if turn['status'] == 'inProgress':
                self.request('turn/interrupt', {'threadId': thread, 'turnId': turn['id']})
                self.wait_turn(thread, turn['id'])

    def close(self):
        if self.process is not None and self.process.poll() is None:
            if os.getpgid(self.process.pid) == os.getpgrp():
                raise RuntimeError('Refusing to terminate the controller process group')
            os.killpg(self.process.pid, signal.SIGTERM)
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait()
        if self.stderr is not None:
            self.stderr.close()
        if self.device_relay is not None:
            self.device_relay.close()
        self.trace.close()


def verify(directory):
    from scripts.ci.codex_adaptation import validate
    validate(directory)
    state = json.loads((directory / 'state.json').read_text())
    if state.get('runtime'):
        from scripts.ci.runtime_adaptation import verify_runtime
        verify_runtime(directory)


def write_diagnostics(directory, iterations, client, status='continue'):
    secrets = getattr(client, 'secrets', []) + auth_secrets()
    result = {'status': status, 'iterations': iterations,
              'modelMessages': getattr(client, 'messages', [])[-10:],
              'threadId': getattr(client, 'thread_id', None),
              'inputDelivered': getattr(client, 'input_delivered', False)}
    result['authenticationRefresh'] = getattr(client, 'authentication_refresh', None)
    account_read = getattr(client, 'account_read_diagnostic', None)
    if account_read is not None:
        result['accountRead'] = account_read
    authentication_failure = getattr(client, 'authentication_failure', None)
    if authentication_failure is not None:
        result['authenticationFailure'] = authentication_failure
    target = directory / 'goal-diagnostics.json'
    temporary = target.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(sanitize(result, secrets), indent=2) + '\n')
    temporary.replace(target)


def account_read_summary(response):
    response_is_dict = isinstance(response, dict)
    account_present = response_is_dict and 'account' in response
    account = response.get('account') if response_is_dict else None
    if not response_is_dict:
        account_shape = 'unavailable'
    elif not account_present:
        account_shape = 'missing'
    elif account is None:
        account_shape = 'null'
    elif isinstance(account, dict):
        account_shape = 'dict'
    else:
        account_shape = 'other'

    account_type = account.get('type') if isinstance(account, dict) else None
    if not isinstance(account_type, str) or account_type not in {'apiKey', 'chatgpt', 'amazonBedrock'}:
        account_type = 'unknown'
    requires_openai_auth = response.get('requiresOpenaiAuth') if response_is_dict else None
    if not isinstance(requires_openai_auth, bool):
        requires_openai_auth = 'unknown'

    return {
        'responseIsDict': response_is_dict,
        'accountFieldPresent': account_present,
        'accountShape': account_shape,
        'accountType': account_type,
        'requiresOpenaiAuth': requires_openai_auth,
    }


def verification_error(directory, error):
    details = str(error)
    if isinstance(error, subprocess.SubprocessError):
        logs = sorted(directory.glob('*.log'), key=lambda p: p.stat().st_mtime, reverse=True)
        for path in logs:
            if path.name not in {'app-server.stderr.log', 'model-output.log'}:
                details += '\n' + path.name + ':\n' + path.read_text(errors='replace')[-4000:]
                break
    return redact(details, auth_secrets())


def persist_turn(directory, client, sequence):
    """Fence continuation until the trusted uploader acknowledges durable storage."""
    if os.environ.get('CODEX_CHECKPOINT_HANDSHAKE') != 'true':
        raise RuntimeError('Per-turn checkpoint uploader is required')
    request = directory / 'checkpoint-request.json'
    acknowledgement = directory / 'checkpoint-ack.json'
    temporary = request.with_suffix('.tmp')
    temporary.write_text(json.dumps({'sequence': sequence, 'threadId': client.thread_id}) + '\n')
    temporary.replace(request)
    while True:
        ack = read_json(acknowledgement, {})
        if ack.get('sequence') == sequence:
            if not ack.get('saved'):
                raise RuntimeError('Checkpoint publication failed: ' + str(ack.get('error', 'unknown')))
            return
        time.sleep(0.2)


def run_goal(client, directory, prompt, resume_context=None, *, verifier=None, checkpoint=None, objective_override=None):
    workspace = directory / 'checkout'
    hidden_repository = str(Path(os.environ.get('GITHUB_WORKSPACE', '/unavailable-repository')) / '.git/HEAD')
    probe = ("from pathlib import Path; import json, tempfile; "
             "report=json.loads(Path('work/analysis/candidate-report.json').read_text()); "
             "assert Path('work/apks').joinpath('candidate-' + report['identity']['apkSha256'] + '.apk').is_file(); "
             "assert Path('work/analysis/candidate-report.json').is_file(); "
             "assert not Path('../state.json').exists(); "
             f"assert not Path({hidden_repository!r}).exists(); "
             "f=tempfile.TemporaryFile(dir='.'); f.write(b'probe'); f.close(); print('sandbox ready')")
    preflight = client.request('command/exec', {
        'command': ['python3', '-c', probe],
        'cwd': str(workspace), 'timeoutMs': 10000,
        'sandboxPolicy': {'type': 'workspaceWrite', 'writableRoots': [str(workspace)], 'networkAccess': False}})
    state_path = directory / 'state.json'
    state = json.loads(state_path.read_text()) if state_path.is_file() else {}
    if preflight.get('exitCode') == 0 and state.get('runtime'):
        preflight = client.request('command/exec', {
            'command': ['adb', 'get-state'], 'cwd': str(workspace), 'timeoutMs': 10000,
            'sandboxPolicy': {'type': 'workspaceWrite', 'writableRoots': [str(workspace)], 'networkAccess': False}})
        if preflight.get('stdout', '').strip() != 'device':
            preflight['exitCode'] = 1
            preflight['stderr'] = 'Dedicated ReDroid is inaccessible from the model sandbox: ' + preflight.get('stderr', '')
    if preflight.get('exitCode') != 0:
        raise RuntimeError('Sandbox preflight failed before model invocation: ' + str(preflight.get('stderr', ''))[:2000])
    from scripts.ci.codex_adaptation import MODEL, REASONING
    native_thread = (resume_context or {}).get('nativeThreadId')
    params = {'cwd': str(directory / 'checkout'), 'model': MODEL,
              'approvalPolicy': 'never', 'sandbox': 'workspace-write',
              'config': {'model_reasoning_effort': REASONING, 'features.multi_agent': False,
                         'features.multi_agent_v2': False, 'features.goals': True}}
    if native_thread:
        params['threadId'] = native_thread
        thread = client.request('thread/resume', params)['thread']['id']
        if thread != native_thread:
            raise RuntimeError('Codex returned a different native thread during resume')
    else:
        params['ephemeral'] = False
        thread = client.request('thread/start', params)['thread']['id']
    client.thread_id = thread
    (directory / 'native-thread.json').write_text(json.dumps({'threadId': thread}) + '\n')
    client.input_delivered = False
    objective = 'Continue adapting the exact candidate APK using only the supplied clean inputs and full instructions. Preserve existing accepted records, rule behavior, and tests; obey allowed edit paths and isolation constraints, and never weaken checks. Continue until your own tests demonstrate completion or an external blocker requires owner input. Before marking the Goal complete or blocked, write work/analysis/adaptation-summary.json with string-array fields completed, blockers, nextActions, questions, written in concise Chinese. Do not create a separate summary turn. Independent acceptance runs after completion; rejection will be returned to this same thread. Do not treat a recoverable test failure as an external blocker.'
    if state.get('runtime'):
        if state.get('appId', state.get('report', {}).get('appId')) == 'qq':
            objective += '\nThis Goal requires independent QQ request identity smoke: the exact APK must naturally send a trusted security command with its Manifest phone AppID when the module is disabled and tablet AppID when enabled. Do not call or register an AppID Getter as an acceptance point. The trusted controller owns socket capture, protocol decoding and acceptance criteria. No account authentication is required or verified. Run impad-smoke for guarded host-side request identity acceptance; ordinary model-side ADB shell requests cannot host the fixed capture.'
        else:
            objective += '\nThis Goal requires ReDroid verification of installed hooks and the application-policy-specific tablet login entry and stable login page.'
        objective += ' Use adb to debug the disposable ReDroid described in work/analysis/runtime-device.json. Static success alone is insufficient. Screenshots and runtime logs from rejected turns will be placed under work/analysis/runtime.'
    objective = objective_override or objective
    verifier = verifier or verify
    checkpoint = checkpoint or persist_turn
    client.request('thread/goal/set', {'threadId': thread, 'objective': objective, 'status': 'paused'})
    feedback = prompt + '\n' + objective
    if resume_context:
        feedback += '\n\nVerified adaptation handoff from the previous scheduled run:\n'
        feedback += json.dumps(resume_context, ensure_ascii=False, sort_keys=True)
    comments = (resume_context or {}).get('ownerComments', [])
    if comments:
        feedback += '\n\nPrioritize these new instructions from the repository owner over earlier adaptation assumptions. '
        feedback += 'Process them in ID order; later instructions supersede conflicting earlier ones. '
        feedback += 'State how each instruction was addressed, or explain the precise blocker. '
        feedback += 'Candidate identity, edit guards and independent acceptance checks remain mandatory.\n'
        feedback += json.dumps(comments, ensure_ascii=False)
    iterations = []
    status = 'continue'
    try:
        write_diagnostics(directory, iterations, client, 'running')
        while True:
            turn = client.request('turn/start', {'threadId': thread, 'model': MODEL, 'effort': REASONING,
                                  'input': [{'type': 'text', 'text': feedback}]})['turn']['id']
            client.input_delivered = True
            client.request('thread/goal/set', {'threadId': thread, 'status': 'active'})
            write_diagnostics(directory, iterations, client, 'running')
            outcome = client.wait_turn(thread, turn)
            goal = client.request('thread/goal/get', {'threadId': thread}).get('goal') or {}
            client.pause(thread)
            goal_status = goal.get('status')
            iterations.append({'turn': turn, 'status': outcome['status'], 'goalStatus': goal_status,
                               'error': outcome.get('error')})
            status = 'running'
            if outcome['status'] != 'completed':
                status = 'interrupted'
            elif goal_status in {'blocked', 'usageLimited', 'budgetLimited'}:
                status = 'blocked' if goal_status == 'blocked' else 'interrupted'
            elif goal_status not in {'active', 'complete'}:
                status = 'interrupted'
            write_diagnostics(directory, iterations, client, status)
            checkpoint(directory, client, len(iterations))
            if status != 'running':
                break
            if goal_status == 'complete':
                try:
                    verifier(directory)
                except (ValueError, OSError, RuntimeError, subprocess.SubprocessError, StopIteration) as error:
                    details = verification_error(directory, error)
                    iterations[-1].update(status='verification-failed', error=details)
                    feedback = 'Independent acceptance rejected the result. Continue this same Goal, fix the cause without weakening checks, and update the terminal summary before completing or blocking.\n' + details
                    (directory / 'checkout/work/analysis/adaptation-summary.json').unlink(missing_ok=True)
                else:
                    iterations[-1]['status'] = 'verified'
                    status = 'verified'
                    client.request('thread/goal/set', {'threadId': thread, 'status': 'complete'})
                    write_diagnostics(directory, iterations, client, status)
                    checkpoint(directory, client, len(iterations) + 1)
                    break
            else:
                feedback = 'Continue the same Goal from the saved turn. Complete the next necessary step; write a terminal summary only when complete or externally blocked.'
        return status
    finally:
        write_diagnostics(directory, iterations, client, status)


def run_model_goal(directory, prompt_path):
    allowed = {'PATH', 'HOME', 'LANG', 'LC_ALL', 'JAVA_HOME', 'ANDROID_HOME', 'ANDROID_SDK_ROOT', 'APP_ID',
               'GRADLE_USER_HOME', 'CODEX_HOME', 'SSL_CERT_FILE', 'TMPDIR', 'WECHATPAD_DEBUG_KEYSTORE', 'IMPAD_ADB_BINARY'}
    env = {key: value for key, value in os.environ.items() if key in allowed}
    client = None
    def interrupted(number, frame):
        raise InterruptedError(f'Controller received signal {number}')
    handlers = {number: signal.signal(number, interrupted) for number in [signal.SIGTERM, signal.SIGINT]}
    try:
        client = AppServer(directory, env)
        client.authentication_refresh = 'requested'
        client.authentication_failure = None
        client.account_read_diagnostic = None
        write_diagnostics(directory, [], client, 'authenticating')
        try:
            # Request managed refresh explicitly before workspace routing begins.
            account_response = client.request('account/read', {'refreshToken': True})
        except Exception:
            client.authentication_failure = 'account_read_rpc'
            client.authentication_refresh = 'failed'
            write_diagnostics(directory, [], client, 'interrupted')
            raise RuntimeError('Codex account/read RPC failed') from None
        client.account_read_diagnostic = account_read_summary(account_response)
        if not client.account_read_diagnostic['responseIsDict']:
            client.authentication_failure = 'account_response_invalid'
        elif (not client.account_read_diagnostic['accountFieldPresent']
              or client.account_read_diagnostic['accountShape'] == 'null'):
            client.authentication_failure = 'account_missing'
        elif client.account_read_diagnostic['accountShape'] != 'dict':
            client.authentication_failure = 'account_invalid'
        elif client.account_read_diagnostic['accountType'] != 'chatgpt':
            client.authentication_failure = 'account_type_mismatch'
        if client.authentication_failure is not None:
            client.authentication_refresh = 'failed'
            write_diagnostics(directory, [], client, 'interrupted')
            raise RuntimeError(
                'Codex authentication preflight failed: ' + client.authentication_failure
            )
        client.authentication_refresh = 'succeeded'
        write_diagnostics(directory, [], client, 'authenticated')
        resume_context = read_json(directory / 'resume-context.json', {})
        status = run_goal(client, directory, prompt_path.read_text(), resume_context)
        output = os.environ.get('GITHUB_OUTPUT')
        if output:
            with Path(output).open('a') as stream:
                stream.write(f"adaptation_ready={str(status == 'verified').lower()}\n")
    except Exception as error:
        path = directory / 'goal-diagnostics.json'
        diagnostics = json.loads(path.read_text()) if path.is_file() else {}
        secrets = getattr(client, 'secrets', []) + auth_secrets()
        diagnostics['controllerError'] = redact(str(error), secrets)
        stderr = directory / 'app-server.stderr.log'
        if stderr.is_file() and getattr(client, 'authentication_failure', None) is None:
            diagnostics['serverErrorTail'] = redact(stderr.read_text(errors='replace')[-2000:], secrets)
        path.write_text(json.dumps(diagnostics, indent=2) + '\n')
        raise
    finally:
        for number, handler in handlers.items():
            signal.signal(number, handler)
        if client is not None:
            client.close()
            from scripts.ci.codex_session_checkpoint import save_session
            save_session(directory, Path(os.environ['CODEX_HOME']),
                         os.environ.get('CODEX_CHECKPOINT_RECIPIENT', ''),
                         os.environ.get('CODEX_CHECKPOINT_IDENTITY', ''))
