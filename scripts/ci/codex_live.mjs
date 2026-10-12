import {spawn, execFileSync} from 'node:child_process';
import {readFile, readdir, mkdir, writeFile, copyFile, statfs} from 'node:fs/promises';
import path from 'node:path';
import {pathToFileURL} from 'node:url';

// Runs in the trusted action process; runtime upload credentials never enter the model environment.
export async function run(core) {
  const controller = process.env.CONTROLLER_ROOT;
  const task = process.env.CODEX_TASK_DIR;
  const prompt = process.env.ADAPTATION_PROMPT;
  if (!prompt) throw new Error('The trusted application policy did not provide an adaptation prompt.');
  const {DefaultArtifactClient} = await import(pathToFileURL(path.join(controller, 'node_modules/@actions/artifact/lib/artifact.js')));
  const artifacts = new DefaultArtifactClient();
  const uploaded = new Set();
  const started = Math.floor(Date.now() / 1000);
  let sequence = 0;
  let stopping = false;
  let signal = null;
  const child = spawn('python3', ['-m', 'scripts.ci.codex_adaptation', 'model',
    '--directory', task, '--prompt', prompt], {cwd: controller, stdio: 'inherit'});
  const finished = new Promise(resolve => {
    child.once('error', error => resolve({error: String(error)}));
    child.once('exit', (code, received) => resolve({code, signal: received}));
  });
  const onSignal = received => {signal = received; stopping = true; child.kill(received);};
  const term = () => onSignal('SIGTERM');
  const interrupt = () => onSignal('SIGINT');
  process.on('SIGTERM', term);
  process.on('SIGINT', interrupt);
  async function snapshot() {
    const chunks = path.join(task, 'trace-chunks');
    const names = (await readdir(chunks).catch(() => [])).filter(name => /^\d+\.jsonl\.age$/.test(name) && !uploaded.has(name)).sort();
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
    for (const name of names) {
      const target = path.join(root, name);
      await copyFile(path.join(chunks, name), target);
      files.push(target);
    }
    await artifacts.uploadArtifact(`${process.env.ARTIFACT_PREFIX || 'impad'}-${process.env.APP_ID || 'wechat'}-codex-live-${process.env.GITHUB_RUN_ATTEMPT}-${sequence}`, files, root, {retentionDays: 14});
    names.forEach(name => uploaded.add(name));
    core.info(`Saved live diagnostics ${sequence}; ${names.length} encrypted conversation chunks.`);
  }
  let outcome;
  try {
    while (!stopping) {
      try {await snapshot();} catch (error) {core.warning(`Live diagnostic upload failed: ${error.message}`);}
      let timer;
      const tick = new Promise(resolve => {timer = setTimeout(() => resolve(null), 60000);});
      outcome = await Promise.race([finished, tick]);
      clearTimeout(timer);
      if (outcome) break;
    }
    outcome ??= await finished;
    try {await snapshot();} catch (error) {core.warning(`Final diagnostic upload failed: ${error.message}`);}
    if (outcome.code !== 0) core.setFailed(`Codex controller exited: ${JSON.stringify(outcome)}`);
  } finally {
    process.off('SIGTERM', term);
    process.off('SIGINT', interrupt);
  }
}
