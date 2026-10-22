import {spawn, execFile, execFileSync} from 'node:child_process';
import {promisify} from 'node:util';
import {readFile, readdir, mkdir, writeFile, copyFile, statfs, rename} from 'node:fs/promises';
import path from 'node:path';
import {pathToFileURL} from 'node:url';

export function adaptationArguments(env = process.env) {
  const appId = env.APP_ID;
  if (!['qq', 'wechat', 'wecom'].includes(appId)) {
    throw new Error('APP_ID must be one of qq, wechat, or wecom.');
  }
  return ['-m', env.CODEX_CONTROLLER_MODULE || 'scripts.ci.codex_adaptation', 'model',
    '--app-id', appId, '--directory', env.CODEX_TASK_DIR, '--prompt', env.ADAPTATION_PROMPT];
}

const diagnosticEnums = {
  status: new Set(['authenticating', 'authenticated', 'running', 'verified', 'blocked', 'interrupted']),
  authenticationRefresh: new Set(['requested', 'succeeded', 'failed']),
  authenticationFailure: new Set(['account_read_rpc', 'account_response_invalid', 'account_missing',
    'account_invalid', 'account_type_mismatch']),
  accountShape: new Set(['unavailable', 'missing', 'null', 'dict', 'other']),
  accountType: new Set(['apiKey', 'chatgpt', 'amazonBedrock', 'unknown']),
  refreshLogOutcome: new Set(['unavailable', 'read_error', 'no_failure_recorded', 'failure_recorded']),
  refreshLogReason: new Set(['expired', 'reused', 'revoked', 'invalid_grant', 'multiple', 'unknown']),
  refreshLogHttpStatus: new Set(['unauthorized', 'bad_request', 'other', 'multiple', 'unknown']),
  authenticationLoadOutcome: new Set(['loaded', 'not_loaded', 'load_error', 'unknown', 'probe_error']),
  authenticationLoadAccountType: new Set(['chatgpt', 'apiKey', 'unknown']),
};

function safeRefreshLogEntry(value) {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) return null;
  const result = {};
  for (const [field, enumName] of [
    ['outcome', 'refreshLogOutcome'], ['reason', 'refreshLogReason'], ['httpStatus', 'refreshLogHttpStatus'],
  ]) {
    if (diagnosticEnums[enumName].has(value[field])) result[field] = value[field];
  }
  return Object.keys(result).length ? result : null;
}

function safeGoalDiagnostics(value) {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) return null;
  const summary = {};
  for (const field of ['status', 'authenticationRefresh', 'authenticationFailure']) {
    if (diagnosticEnums[field].has(value[field])) summary[field] = value[field];
  }
  if (value.authenticationRefreshLog !== null && typeof value.authenticationRefreshLog === 'object' &&
      !Array.isArray(value.authenticationRefreshLog)) {
    const authenticationRefreshLog = {};
    for (const field of ['beforeRefresh', 'afterRefresh']) {
      const entry = safeRefreshLogEntry(value.authenticationRefreshLog[field]);
      if (entry) authenticationRefreshLog[field] = entry;
    }
    if (Object.keys(authenticationRefreshLog).length) {
      summary.authenticationRefreshLog = authenticationRefreshLog;
    }
  }
  if (value.accountRead !== null && typeof value.accountRead === 'object' && !Array.isArray(value.accountRead)) {
    const accountRead = {};
    for (const field of ['responseIsDict', 'accountFieldPresent']) {
      if (typeof value.accountRead[field] === 'boolean') accountRead[field] = value.accountRead[field];
    }
    for (const [field, enumName] of [['accountShape', 'accountShape'], ['accountType', 'accountType']]) {
      if (diagnosticEnums[enumName].has(value.accountRead[field])) accountRead[field] = value.accountRead[field];
    }
    if (typeof value.accountRead.requiresOpenaiAuth === 'boolean' ||
        value.accountRead.requiresOpenaiAuth === 'unknown') {
      accountRead.requiresOpenaiAuth = value.accountRead.requiresOpenaiAuth;
    }
    if (Object.keys(accountRead).length) summary.accountRead = accountRead;
  }
  if (value.authenticationLoad !== null && typeof value.authenticationLoad === 'object' &&
      !Array.isArray(value.authenticationLoad)) {
    const authenticationLoad = {};
    if (diagnosticEnums.authenticationLoadOutcome.has(value.authenticationLoad.outcome)) {
      authenticationLoad.outcome = value.authenticationLoad.outcome;
    }
    if (diagnosticEnums.authenticationLoadAccountType.has(value.authenticationLoad.accountType)) {
      authenticationLoad.accountType = value.authenticationLoad.accountType;
    }
    const exitCode = value.authenticationLoad.exitCode;
    authenticationLoad.exitCode = exitCode === null ||
      (Number.isInteger(exitCode) && exitCode >= 0 && exitCode <= 255) ? exitCode : null;
    if (Object.keys(authenticationLoad).length) summary.authenticationLoad = authenticationLoad;
  }
  return Object.keys(summary).length ? summary : null;
}

// Runs in the trusted action process; runtime upload credentials never enter the model environment.
export async function run(core) {
  const appId = process.env.APP_ID;
  const adaptationArgs = adaptationArguments(process.env);
  const controller = process.env.CONTROLLER_ROOT;
  const task = process.env.CODEX_TASK_DIR;
  const prompt = process.env.ADAPTATION_PROMPT;
  if (!prompt) throw new Error('The trusted application policy did not provide an adaptation prompt.');
  const {DefaultArtifactClient} = await import(pathToFileURL(path.join(controller, 'node_modules/@actions/artifact/lib/artifact.js')));
  const artifacts = new DefaultArtifactClient();
  const uploaded = new Set();
  const started = Math.floor(Date.now() / 1000);
  let sequence = 0;
  let checkpointSequence = 0;
  let stopping = false;
  let signal = null;
  let acknowledged = 0;
  let nextDiagnostics = 0;
  let diagnosticsUpload = null;
  const child = spawn('python3', adaptationArgs, {cwd: controller, stdio: 'inherit', env: {...process.env, CODEX_CHECKPOINT_HANDSHAKE: 'true'}});
  const finished = new Promise(resolve => {
    child.once('error', error => resolve({error: String(error)}));
    child.once('exit', (code, received) => resolve({code, signal: received}));
  });
  const onSignal = received => {signal = received; stopping = true; child.kill(received);};
  const term = () => onSignal('SIGTERM');
  const interrupt = () => onSignal('SIGINT');
  process.on('SIGTERM', term);
  process.on('SIGINT', interrupt);
  async function checkpoint() {
    if (!process.env.ADAPTATION_ISSUE) throw new Error('Live checkpoints require a claimed adaptation issue.');
    const threadFile = path.join(task, 'native-thread.json');
    if (!(await readFile(threadFile).then(() => true).catch(() => false))) return false;
    const output = path.join(task, 'live-checkpoints', String(process.env.GITHUB_RUN_ATTEMPT || '1'), String(++checkpointSequence));
    await mkdir(output, {recursive: true, mode: 0o700});
    execFileSync('python3', ['-m', 'scripts.ci.codex_session_checkpoint', 'save',
      '--directory', task, '--home', process.env.CODEX_HOME], {cwd: controller, stdio: 'pipe'});
    execFileSync('python3', ['-m', 'scripts.ci.adaptation_checkpoint', 'capture',
      '--directory', task], {cwd: controller, stdio: 'pipe'});
    const names = ['candidate-report.json', 'checkpoint.patch', 'checkpoint.json', 'handoff.json',
      'native-session.tar.age', 'goal-diagnostics.json', 'adaptation.patch', 'runtime-report.json'];
    const files = [];
    for (const name of names) {
      const source = path.join(task, name);
      try {
        await copyFile(source, path.join(output, name));
        files.push(path.join(output, name));
      } catch (error) {
        if (error.code !== 'ENOENT') throw error;
      }
    }
    if (!files.includes(path.join(output, 'native-session.tar.age')) ||
        !files.includes(path.join(output, 'checkpoint.json'))) {
      throw new Error('Live native-session checkpoint is incomplete');
    }
    const report = JSON.parse(await readFile(path.join(task, 'candidate-report.json'), 'utf8'));
    const diagnostics = JSON.parse(await readFile(path.join(task, 'goal-diagnostics.json'), 'utf8'));
    const checkpointStatus = ['running', 'verified', 'blocked', 'interrupted'].includes(diagnostics.status) ? diagnostics.status : 'interrupted';
    const artifactName = `${process.env.ARTIFACT_PREFIX || 'impad'}-${appId}-codex-adaptation-live-${process.env.GITHUB_RUN_ATTEMPT || '1'}-${checkpointSequence}-${report.identity.apkSha256.toLowerCase()}-${checkpointStatus}`;
    await artifacts.uploadArtifact(artifactName, files, output, {retentionDays: 90});
    if (process.env.ADAPTATION_ISSUE !== '0') execFileSync('python3', ['-m', 'scripts.ci.codex_task', 'checkpoint', '--app-id', process.env.APP_ID,
      '--report', path.join(task, 'candidate-report.json'), '--directory', task,
      '--issue', process.env.ADAPTATION_ISSUE, '--artifact-name', artifactName, '--live'],
      {cwd: controller, stdio: 'pipe'});
    const pointer = path.join(task, 'last-published-checkpoint.json');
    await writeFile(pointer + '.tmp', JSON.stringify({directory: output, artifact: artifactName}), {mode: 0o600});
    await rename(pointer + '.tmp', pointer);
    core.info(`Saved resumable checkpoint ${checkpointSequence} as ${artifactName}.`);
    return true;
  }
  async function snapshot() {
    const chunks = path.join(task, 'trace-chunks');
    const root = path.join(task, 'live', String(++sequence));
    await mkdir(root, {recursive: true, mode: 0o700});
    const read = name => readFile(name, 'utf8').catch(() => 'unavailable');
    const disk = await statfs(task);
    const cgroupMembership = await read(`/proc/${child.pid}/cgroup`);
    const membership = cgroupMembership.split('\n').map(line => line.split(':'));
    const unified = membership.find(parts => parts[0] === '0' && parts[1] === '');
    const legacy = membership.find(parts => parts[1]?.split(',').includes('memory'));
    const cgroupRoot = unified ? '/sys/fs/cgroup' + unified[2] :
      legacy ? '/sys/fs/cgroup/memory' + legacy[2] : '/sys/fs/cgroup';
    const cgroupCounters = {};
    for (const name of ['memory.events', 'memory.current', 'memory.max',
      'memory.oom_control', 'memory.failcnt', 'memory.limit_in_bytes', 'memory.usage_in_bytes']) {
      cgroupCounters[name] = await read(path.join(cgroupRoot, name));
    }
    const telemetry = {disk: {blockSize: disk.bsize, availableBlocks: disk.bavail},
      processes: execFileSync('ps', ['-eo', 'pid,ppid,rss,comm'], {encoding: 'utf8'}), timestamp: new Date().toISOString(), controllerPid: child.pid, signal,
      cgroupMembership, cgroupCounters,
      memory: await read('/proc/meminfo'), memoryPressure: await read('/proc/pressure/memory'),
      cgroupMemoryEvents: await read('/sys/fs/cgroup/memory.events'),
      cgroupMemoryCurrent: await read('/sys/fs/cgroup/memory.current'),
      cgroupMemoryMax: await read('/sys/fs/cgroup/memory.max')};
    const destination = path.join(root, 'runner-health.json');
    await writeFile(destination, JSON.stringify(telemetry, null, 2), {mode: 0o600});
    const files = [destination];
    try {
      const diagnostics = JSON.parse(await readFile(path.join(task, 'goal-diagnostics.json'), 'utf8'));
      const safeDiagnostics = safeGoalDiagnostics(diagnostics);
      if (safeDiagnostics) {
        const safeDestination = path.join(root, 'goal-diagnostics.json');
        await writeFile(safeDestination, JSON.stringify(safeDiagnostics, null, 2) + '\n', {mode: 0o600});
        files.push(safeDestination);
      }
    } catch {}
    const logs = {};
    try {
      logs.kernel = execFileSync('sudo', ['-n', 'journalctl', '-k', '--since', `@${started}`,
        '--no-pager', '-o', 'short-iso'], {encoding: 'utf8', timeout: 5000, maxBuffer: 4 * 1024 * 1024, stdio: ['ignore', 'pipe', 'ignore']});
    } catch {logs.kernel = 'unavailable';}
    // Runner service logs can contain credentials; they enter only the encrypted file.
    const processRows = telemetry.processes.split('\n').map(line => line.trim().split(/\s+/));
    const runnerPid = processRows.find(row => row[3] === 'Runner.Worker')?.[0];
    if (runnerPid) {
      const {readlink} = await import('node:fs/promises');
      try {
        const executable = await readlink(`/proc/${runnerPid}/exe`);
        const diagnostics = path.resolve(path.dirname(executable), '../_diag');
        const names = (await readdir(diagnostics)).filter(name => /^(Worker|Runner)_.*\.log$/.test(name)).sort();
        for (const name of names.slice(-3)) logs[name] = (await read(path.join(diagnostics, name))).slice(-256 * 1024);
      } catch {logs.runner = 'unavailable';}
    }
    const encryptedLogs = execFileSync('age', ['--encrypt', '-r', process.env.CODEX_TRACE_RECIPIENT], {
      input: JSON.stringify({timestamp: telemetry.timestamp, logs}), timeout: 10000,
      maxBuffer: 8 * 1024 * 1024, stdio: ['pipe', 'pipe', 'ignore']});
    const logFile = path.join(root, 'runner-logs.json.age');
    await writeFile(logFile, encryptedLogs, {mode: 0o600});
    files.push(logFile);
    try {
      const observation = path.join(root, 'session-observation.tar.age');
      const result = await promisify(execFile)('python3', ['-m', 'scripts.ci.codex_observation',
        '--directory', task, '--home', process.env.CODEX_HOME, '--destination', observation],
        {cwd: controller, timeout: 120000, maxBuffer: 1024 * 1024});
      if (JSON.parse(result.stdout).saved) files.push(observation);
    } catch (error) {
      core.warning(`Session observation capture failed: ${error.message}`);
    }
    const names = (await readdir(chunks).catch(() => [])).filter(name => /^\d+\.jsonl\.age$/.test(name) && !uploaded.has(name)).sort();
    for (const name of names) {
      const target = path.join(root, name);
      await copyFile(path.join(chunks, name), target);
      files.push(target);
    }
    await artifacts.uploadArtifact(`${process.env.ARTIFACT_PREFIX || 'impad'}-${appId}-codex-live-${process.env.GITHUB_RUN_ATTEMPT}-${sequence}`, files, root, {retentionDays: 14});
    names.forEach(name => uploaded.add(name));
    core.info(`Saved live diagnostics ${sequence}; ${names.length} encrypted conversation chunks.`);
  }
  let outcome;
  try {
    while (!stopping) {
      if (!diagnosticsUpload && Date.now() >= nextDiagnostics) {
        nextDiagnostics = Date.now() + 60000;
        diagnosticsUpload = snapshot()
          .catch(error => core.warning(`Live diagnostic upload failed: ${error.message}`))
          .finally(() => {diagnosticsUpload = null;});
      }
      const request = await readFile(path.join(task, 'checkpoint-request.json'), 'utf8')
        .then(JSON.parse).catch(() => null);
      if (request && request.sequence > acknowledged) {
        let error;
        let saved = false;
        for (let attempt = 0; attempt < 3 && !saved; attempt++) {
          try {saved = await checkpoint();} catch (failure) {error = failure.message;}
        }
        const ack = path.join(task, 'checkpoint-ack.json');
        await writeFile(ack + '.tmp', JSON.stringify({sequence: request.sequence, saved, error}), {mode: 0o600});
        await rename(ack + '.tmp', ack);
        acknowledged = request.sequence;
      }
      let timer;
      const tick = new Promise(resolve => {timer = setTimeout(() => resolve(null), 1000);});
      outcome = await Promise.race([finished, tick]);
      clearTimeout(timer);
      if (outcome) break;
    }
    outcome ??= await finished;
    if (diagnosticsUpload) await diagnosticsUpload;
    try {await snapshot();} catch (error) {core.warning(`Final diagnostic upload failed: ${error.message}`);}
    if (outcome.code !== 0) core.setFailed(`Codex controller exited: ${JSON.stringify(outcome)}`);
  } finally {
    process.off('SIGTERM', term);
    process.off('SIGINT', interrupt);
  }
}
