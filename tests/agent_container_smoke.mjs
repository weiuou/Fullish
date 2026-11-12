/** Real Linux Dim + rutis + private Unix RPC, with local model and bridge fixtures only. */
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { dirname, resolve } from 'node:path';
import { mkdtemp, mkdir, writeFile, readFile, rm, access } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { createServer } from 'node:http';
import { createConnection } from 'node:net';
import { spawn } from 'node:child_process';
import { randomUUID, createHash } from 'node:crypto';
import { connectControl, stopExpiredSessions } from '../deploy/agent/control.mjs';

assert.equal(process.platform, 'linux');
const root = await mkdtemp(resolve(tmpdir(), 'fullish-dim-container-'));
const require = createRequire('/opt/fullish-agent/package.json');
const packagePath = require.resolve('dimcode-alpha/package.json');
const nativePackage = createRequire(packagePath).resolve(`dimcode-linux-${process.arch}/package.json`);
const binary = resolve(dirname(nativePackage), 'bin/dimcode');
const token = 'synthetic-container-bridge-token-123456789';
let candidate, sessionId, modelRequests = 0, daemon, connection;
let forbidden = false, forbiddenRequests = 0;
let hold = false, heldRequests = 0;
const pending = new Map();
let sequence = 0, buffer = '';
const bridge = createServer(async (req, res) => {
  let body = '';
  for await (const chunk of req) body += chunk;
  assert.equal(req.headers.authorization, `Bearer ${token}`);
  const call = JSON.parse(body);
  assert.equal(call.session_id, sessionId);
  assert.equal(call.name, 'fullish_reply');
  candidate = call.arguments.text;
  res.writeHead(200, { 'Content-Type': 'application/json' });
  res.end(JSON.stringify({ content: [{ type: 'text', text: 'registered, not sent' }] }));
});
const model = createServer(async (req, res) => {
  let body = '';
  for await (const chunk of req) body += chunk;
  const request = JSON.parse(body);
  if (hold) { heldRequests++; return; }
  modelRequests++;
  assert.deepEqual((request.tools || []).map(tool => tool.function.name), forbidden ? [] : ['fullish_reply']);
  if (forbidden) forbiddenRequests++;
  const first = forbidden ? forbiddenRequests === 1 : modelRequests === 1;
  const delta = forbidden && first ? { role: 'assistant', tool_calls: [{ index: 0,
    id: 'forbidden-exec', type: 'function', function: { name: 'exec',
      arguments: JSON.stringify({ command: `touch ${resolve(root, 'forbidden')}` }) } }] }
    : first ? { role: 'assistant', tool_calls: [{ index: 0,
    id: 'local-final', type: 'function', function: { name: 'fullish_reply',
      arguments: JSON.stringify({ text: '容器内插件验收成功。' }) } }] }
    : { role: 'assistant', content: 'This final model sentence does not replace the registered candidate.' };
  res.writeHead(200, { 'Content-Type': 'text/event-stream' });
  for (const [value, finish] of [[delta, null], [{}, first ? 'tool_calls' : 'stop']]) {
    res.write('data: ' + JSON.stringify({ id: 'fixture', object: 'chat.completion.chunk',
      model: 'probe', choices: [{ index: 0, delta: value, finish_reason: finish }] }) + '\n\n');
  }
  res.end('data: [DONE]\n\n');
});
const listen = server => new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const pause = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));
function call(method, params) {
  const id = ++sequence;
  return new Promise((resolve, reject) => {
    const timeout = setTimeout(() => { pending.delete(id); reject(Error('RPC timeout')); }, 20_000);
    pending.set(id, { resolve: result => { clearTimeout(timeout); resolve(result); },
                      reject: error => { clearTimeout(timeout); reject(error); } });
    connection.write(JSON.stringify({ jsonrpc: '2.0', id, method, params }) + '\n');
  });
}
try {
  await listen(bridge);
  await listen(model);
  await mkdir(resolve(root, 'home'));
  const config = { defaultModel: { providerId: 'fixture', modelId: 'probe' }, providers: [{
    id: 'fixture', kind: 'openai-chat', baseUrl: `http://127.0.0.1:${model.address().port}/v1`,
    apiKeyEnv: 'LOCAL_FIXTURE_KEY', models: [{ id: 'probe', contextWindow: 32000,
      maxOutputTokens: 4096, config: { maxTokens: 1024 } }],
  }] };
  await writeFile(resolve(root, 'model.json'), JSON.stringify(config));
  const endpoint = resolve(root, 'rpc.sock');
  daemon = spawn(binary, ['app-server', '--home', resolve(root, 'home'), '--model-config', resolve(root, 'model.json'),
    '--unix', endpoint, '--no-ws', '--threads', '2', '--defaults', '/opt/fullish-plugin/defaults.json',
    '--node', '/usr/local/bin/node', '--js-runtime', '/opt/fullish-plugin/node_modules/@arcships/rutis-runtime'],
  { stdio: ['ignore', 'ignore', 'inherit'], env: { PATH: process.env.PATH, HOME: root, LANG: 'C.UTF-8',
    LOCAL_FIXTURE_KEY: 'synthetic', FULLISH_AGENT_BRIDGE_TOKEN: token,
    FULLISH_AGENT_BRIDGE_URL: `http://127.0.0.1:${bridge.address().port}/` } });
  for (let attempt = 0; attempt < 150; attempt++) {
    connection = await new Promise(resolve => {
      const socket = createConnection(endpoint);
      socket.once('error', () => { socket.destroy(); resolve(null); });
      socket.once('connect', () => resolve(socket));
    });
    if (connection) break;
    if (daemon.exitCode !== null) throw Error('Dim daemon exited before readiness');
    await pause(100);
  }
  assert.ok(connection, 'private Unix socket became ready');
  connection.on('data', chunk => {
    buffer += chunk.toString('utf8');
    while (buffer.includes('\n')) {
      const end = buffer.indexOf('\n');
      const message = JSON.parse(buffer.slice(0, end));
      buffer = buffer.slice(end + 1);
      const waiter = pending.get(message.id);
      if (waiter) {
        pending.delete(message.id);
        if (message.error) waiter.reject(Error(message.error.data?.appError || 'RPC failed'));
        else waiter.resolve(message.result);
      }
    }
  });
  const hello = await call('hello', { protocolVersion: 3, clientKind: 'other' });
  assert.equal(hello.serverVersion, '0.1.0-alpha.20');
  let catalog;
  for (let attempt = 0; attempt < 100; attempt++) {
    catalog = await call('plugin.list', {});
    if (catalog.some(item => item.name === 'fullish-visualize' && item.status === 'running')) break;
    await pause(100);
  }
  assert.ok(catalog.some(item => item.name === 'fullish-visualize' && item.status === 'running'),
    JSON.stringify(catalog.map(item => ({ name: item.name, status: item.status }))));
  const created = await call('session.create', { cwd: root, tools: ['fullish_reply'], interactive: false,
    permissionPolicy: { preset: 'read-only', allowedTools: ['fullish_reply'] }, systemPrompt: 'Synthetic integration fixture.' });
  sessionId = created.sessionId;
  await call('session.attach', { sessionId });
  await call('session.submit', { sessionId, branchId: 'main', submissionId: randomUUID(), text: 'Synthetic input.' });
  await call('session.idle', { sessionId, branchId: 'main' });
  const history = await call('session.history', { sessionId, branchId: 'main', limit: 128 });
  assert.ok(history.events.some(event => event.type === 'turn' && event.name === 'end' && event.data.status === 'completed'));
  assert.equal(candidate, '容器内插件验收成功。');
  assert.equal(modelRequests, 2);
  await call('session.close', { sessionId });
  assert.equal((await call('session.delete', { sessionId, requireInactive: true })).deleted, true);
  forbidden = true;
  sessionId = (await call('session.create', { cwd: root, tools: [], interactive: false,
    permissionPolicy: { preset: 'read-only', allowedTools: [] } })).sessionId;
  await call('session.attach', { sessionId });
  await call('session.submit', { sessionId, branchId: 'main', submissionId: randomUUID(), text: 'Synthetic negative test.' });
  await call('session.idle', { sessionId, branchId: 'main' });
  const denied = await call('session.history', { sessionId, branchId: 'main', limit: 128 });
  assert.equal(forbiddenRequests, 1, 'fixture sent one unoffered exec call');
  assert.ok(denied.events.some(event => event.type === 'turn' && event.name === 'end'
    && event.data.status === 'failed'), 'native provider rejected the unoffered tool');
  await assert.rejects(access(resolve(root, 'forbidden')), { code: 'ENOENT' });
  await call('session.close', { sessionId });
  assert.equal((await call('session.delete', { sessionId, requireInactive: true })).deleted, true);
  hold = true;
  const observer = await connectControl(endpoint);
  try {
    for (const detached of [false, true]) {
      sessionId = (await call('session.create', { cwd: root, tools: [], interactive: false,
        permissionPolicy: { preset: 'read-only', allowedTools: [] } })).sessionId;
      await call('session.attach', { sessionId });
      await call('session.submit', { sessionId, branchId: 'main', submissionId: randomUUID(), text: 'Synthetic hanging model.' });
      const expected = detached ? 2 : 1;
      for (let i = 0; i < 100 && heldRequests < expected; i++) await pause(50);
      assert.equal(heldRequests, expected, 'real provider request started');
      if (detached) await call('session.detach', { sessionId });
      assert.equal(await stopExpiredSessions(observer.call, Date.now() + (detached ? 3000 : 91_000)), 1);
      const catalog = await observer.call('session.catalog', { limit: 128, withPreview: false });
      assert.equal(catalog.sessions.find(session => session.sessionId === sessionId).running, false);
      await call('session.close', { sessionId });
      assert.equal((await call('session.delete', { sessionId, requireInactive: true })).deleted, true);
    }
  } finally { observer.close(); }
  console.log(JSON.stringify({ linux_runtime: true, private_unix: true, native_plugin: true,
    candidate_bridge: true, hidden_exec_refused: true, orphan_and_expired_stopped: true,
    binary_sha256: createHash('sha256').update(await readFile(binary)).digest('hex'),
    model_requests: modelRequests + heldRequests, version: hello.serverVersion }));
  await call('daemon.shutdown', {});
} finally {
  connection?.destroy();
  for (const waiter of pending.values()) waiter.reject(Error('closed'));
  if (daemon && daemon.exitCode === null) {
    daemon.kill('SIGTERM');
    await Promise.race([new Promise(resolve => daemon.once('exit', resolve)), pause(5000)]);
    if (daemon.exitCode === null) daemon.kill('SIGKILL');
  }
  model.closeAllConnections();
  await new Promise(resolve => model.close(resolve));
  await new Promise(resolve => bridge.close(resolve));
  assert.ok(root.startsWith(resolve(tmpdir(), 'fullish-dim-container-')));
  await rm(root, { recursive: true, force: true });
}
