/** Dedicated runtime entrypoint: no bot, wallet, personal-home or arbitrary workspace mounts. */
import { createRequire } from 'node:module';
import { dirname, resolve } from 'node:path';
import { readFile, writeFile, mkdir, lstat } from 'node:fs/promises';
import { spawn, spawnSync } from 'node:child_process';
import { createConnection } from 'node:net';
import { connectControl, stopExpiredSessions } from './control.mjs';

const require = createRequire(import.meta.url);
const packagePath = require.resolve('dimcode-alpha/package.json');
const binaryPackage = createRequire(packagePath).resolve(`dimcode-linux-${process.arch}/package.json`);
const binary = resolve(dirname(binaryPackage), 'bin/dimcode');
const version = spawnSync(binary, ['--version'], { encoding: 'utf8' });
if (version.status !== 0 || version.stdout.trim() !== 'dimcode 0.1.0-alpha.20') {
  throw Error('dim_version_mismatch');
}
const endpoint = '/run/fullish-agent/rpc.sock';

async function check() {
  const info = await lstat(endpoint);
  if (!info.isSocket() || info.uid !== process.getuid() || (info.mode & 0o077)) {
    throw Error('private_agent_socket_required');
  }
  await new Promise((resolve, reject) => {
    const connection = createConnection(endpoint);
    let buffered = '';
    connection.setTimeout(3000, () => connection.destroy(Error('agent_probe_timeout')));
    connection.on('error', reject);
    connection.on('connect', () => connection.write(JSON.stringify({ jsonrpc: '2.0', id: 1,
      method: 'hello', params: { protocolVersion: 3, clientKind: 'other' } }) + '\n'));
    connection.on('data', chunk => {
      buffered += chunk.toString('utf8');
      if (buffered.length > 64 * 1024) return connection.destroy(Error('agent_probe_limit'));
      try {
        while (buffered.includes('\n')) {
          const end = buffered.indexOf('\n');
          const reply = JSON.parse(buffered.slice(0, end));
          buffered = buffered.slice(end + 1);
          if (reply.id === 1) {
            if (reply.result?.protocolVersion !== 3 || reply.result?.serverVersion !== '0.1.0-alpha.20') {
              throw Error('agent_probe_mismatch');
            }
            connection.write(JSON.stringify({ jsonrpc: '2.0', id: 2,
              method: 'plugin.list', params: {} }) + '\n');
          } else if (reply.id === 2) {
            if (!Array.isArray(reply.result) || !reply.result.some(item =>
              item.name === 'fullish-visualize' && item.status === 'running' && item.off === false)) {
              throw Error('agent_plugin_not_ready');
            }
            connection.end();
            resolve();
          }
        }
      } catch (error) { connection.destroy(error); }
    });
    connection.on('end', () => reject(Error('agent_probe_closed')));
  });
}

if (process.argv[2] === '--check') {
  await check();
} else {
  const home = '/var/lib/fullish-agent';
  const modelKey = (await readFile('/run/secrets/dim-model-key', 'utf8')).trim();
  const bridgeToken = (await readFile('/run/secrets/dim-bridge-token', 'utf8')).trim();
  if (!modelKey || bridgeToken.length < 32) throw Error('agent_credentials_missing');
  const base = new URL(process.env.DIM_MODEL_BASE_URL || 'http://127.0.0.1:8317/v1');
  if (!['http:', 'https:'].includes(base.protocol) || base.username || base.password || base.search || base.hash) {
    throw Error('invalid_model_base');
  }
  const modelId = process.env.DIM_MODEL_ID;
  if (!modelId || modelId.length > 200) throw Error('model_id_required');
  await mkdir(home, { recursive: true, mode: 0o700 });
  const config = { defaultModel: { providerId: 'fullish', modelId }, providers: [{
    id: 'fullish', kind: 'openai-chat', baseUrl: base.href.replace(/\/$/, ''),
    apiKeyEnv: 'FULLISH_AGENT_MODEL_KEY', models: [{ id: modelId, contextWindow: 32000,
      maxOutputTokens: 8192, config: { maxTokens: 8192 } }],
  }] };
  await writeFile(resolve(home, 'model.json'), JSON.stringify(config), { mode: 0o600 });
  const child = spawn(binary, ['app-server', '--home', home, '--model-config', resolve(home, 'model.json'),
    '--unix', endpoint, '--no-ws', '--threads', '2', '--defaults', '/opt/fullish-plugin/defaults.json',
    '--node', '/usr/local/bin/node', '--js-runtime', '/opt/fullish-plugin/node_modules/@arcships/rutis-runtime'],
  { stdio: 'inherit', env: {
    PATH: '/usr/local/bin:/usr/bin:/bin', HOME: home, LANG: 'C.UTF-8', RUST_LOG: 'error',
    FULLISH_AGENT_MODEL_KEY: modelKey, FULLISH_AGENT_BRIDGE_TOKEN: bridgeToken,
    FULLISH_AGENT_BRIDGE_SOCKET: '/run/fullish-agent/bridge.sock',
  } });
  for (const signal of ['SIGTERM', 'SIGINT']) process.on(signal, () => child.kill(signal));
  child.on('error', () => { process.exitCode = 1; });
  child.on('exit', (code, signal) => { process.exitCode = code ?? (signal ? 1 : 0); });
  let stopping = false;
  child.once('exit', () => { stopping = true; });
  const pause = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));
  // The controller can disappear while the daemon remains healthy. Independently stop
  // orphan/expired sessions; an unresponsive control plane terminates this dedicated daemon.
  (async () => {
    let control;
    const readyBy = Date.now() + 20_000;
    try {
      while (!stopping && !control) {
        try { control = await connectControl(endpoint); }
        catch {
          if (Date.now() >= readyBy) throw Error('agent_watchdog_start_timeout');
          await pause(200);
        }
      }
      while (!stopping) {
        await stopExpiredSessions(control.call);
        await pause(500);
      }
    } catch {
      if (!stopping) {
        process.stderr.write('agent_watchdog_failed\n');
        child.kill('SIGTERM');
        const kill = setTimeout(() => { if (!stopping) child.kill('SIGKILL'); }, 5000);
        child.once('exit', () => clearTimeout(kill));
      }
    } finally { control?.close(); }
  })().catch(() => { process.exitCode = 1; });
}
