/** Real companion + Fullish + Chromium, synthetic model/QQ only; no sandbox socket. */
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { createHash, randomUUID } from 'node:crypto';
import { mkdtemp, writeFile, chmod, chown, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { resolve } from 'node:path';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';

assert.equal(process.platform, 'linux');
assert.equal(process.env.FULLISH_DIRECT_AGENT_CI, '1', 'Use only an isolated Docker CI environment');
const execute = promisify(execFile);
const docker = async (...args) => (await execute('docker', args,
  { timeout: 240000, maxBuffer: 16 * 1024 * 1024 })).stdout;
const root = await mkdtemp(resolve(tmpdir(), 'fullish-direct-ci-'));
const prefix = 'fullish-direct-' + randomUUID().replaceAll('-', '').slice(0, 12);
const volumes = ['rpc', 'work', 'home'].map(name => prefix + '-' + name);
const [botImage = 'fullish:ci', agentImage = 'fullish-agent:integration-ci'] = process.argv.slice(2);
let requests = 0;
const pixels = new Set();
const reply = '插件直接绘图验收成功。';
const model = createServer(async (req, res) => {
  try {
    assert.equal(req.url, '/v1/chat/completions');
    assert.equal(req.headers.authorization, 'Bearer synthetic-direct-key');
    let body = '';
    for await (const chunk of req) body += chunk;
    const input = JSON.parse(body);
    requests++;
    const tools = (input.tools || []).map(tool => tool.function.name);
    const completed = input.messages.filter(item => item.role === 'tool').map(item => item.tool_call_id);
    let message;
    if (!tools.length) message = { role: 'assistant', content: '本地 Dim 协议验收成功。' };
    else {
      assert.deepEqual(new Set(tools), new Set(['visualize_guide', 'visualize_render', 'fullish_reply']));
      const count = completed.filter(id => id.startsWith('fixture-render-')).length;
      if (completed.includes('fixture-final')) message = { role: 'assistant', content: 'Unused model tail.' };
      else {
        let name = 'fullish_reply', id = 'fixture-final', args = { text: reply };
        if (count < 2) {
          name = 'visualize_render'; id = 'fixture-render-' + (count + 1);
          args = { format: 'svg', width: 1000, height: 650,
            source: '<svg xmlns="http://www.w3.org/2000/svg" width="1000" height="650" viewBox="0 0 1000 650">'
              + `<rect width="1000" height="650" fill="${count ? '#ccfbf1' : '#e0f2fe'}"/>`
              + '<text x="40" y="100" font-size="32">Dim → 插件直接绘图 → QQ 确认</text></svg>' };
        } else {
          const material = JSON.stringify(input);
          const images = [...material.matchAll(/data:image\/png;base64,([A-Za-z0-9+/=]+)/g)];
          assert.ok(images.length >= 2, 'actual native model receives both PNGs');
          for (const image of images) pixels.add(createHash('sha256').update(Buffer.from(image[1], 'base64')).digest('hex'));
          args.artifact_id = [...material.matchAll(/img_[a-f0-9]{32}/g)].at(-1)[0];
        }
        message = { role: 'assistant', tool_calls: [{ index: 0, id, type: 'function',
          function: { name, arguments: JSON.stringify(args) } }] };
      }
    }
    res.writeHead(200, { 'Content-Type': 'text/event-stream' });
    for (const [delta, finish] of [[message, null], [{}, message.tool_calls ? 'tool_calls' : 'stop']]) {
      res.write('data: ' + JSON.stringify({ id: 'fixture', object: 'chat.completion.chunk',
        model: 'fixture', choices: [{ index: 0, delta, finish_reason: finish }] }) + '\n\n');
    }
    res.end('data: [DONE]\n\n');
  } catch (error) { res.writeHead(500); res.end('fixture assertion failed'); console.error(error); }
});
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
try {
  await chown(root, 10001, 10001);
  for (const [name, value] of [['model-key', 'synthetic-direct-key'],
    ['bridge-token', 'synthetic-direct-bridge-token-123456789']]) {
    const path = resolve(root, name);
    await writeFile(path, value, { mode: 0o600 });
    await chown(path, 10001, 10001);
  }
  await new Promise(resolve => model.listen(0, '127.0.0.1', resolve));
  const mount = (name, target) => ['--mount', `type=bind,source=${resolve(root, name)},target=${target},readonly`];
  const limits = ['--network', 'host', '--read-only', '--tmpfs', '/tmp:rw,noexec,nosuid,size=128m',
    '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true', '--pids-limit', '256'];
  await docker('run', '-d', '--init', '--name', prefix, ...limits,
    '--memory', '1g', '--memory-swap', '1g', '--cpus', '1',
    '--volume', volumes[0] + ':/run/fullish-agent', '--volume', volumes[1] + ':/var/lib/fullish-agent-work',
    '--volume', volumes[2] + ':/var/lib/fullish-agent',
    ...mount('model-key', '/run/secrets/dim-model-key'), ...mount('bridge-token', '/run/secrets/dim-bridge-token'),
    '--env', 'DIM_MODEL_ID=fixture', '--env', `DIM_MODEL_BASE_URL=http://127.0.0.1:${model.address().port}/v1`, agentImage);
  let ready = false;
  for (let attempt = 0; attempt < 90; attempt++) {
    const state = JSON.parse(await docker('inspect', '--format', '{{json .State}}', prefix));
    assert.equal(state.Running, true, 'dedicated companion stays alive');
    if (state.Health?.Status === 'healthy') { ready = true; break; }
    await pause(1000);
  }
  assert.equal(ready, true, 'actual companion becomes healthy');
  const output = await docker('run', '--rm', '--init', '--name', prefix + '-client', ...limits,
    '--memory', '512m', '--volume', volumes[0] + ':/run/fullish-agent',
    '--volume', volumes[1] + ':/var/lib/fullish-agent-work',
    ...mount('bridge-token', '/run/secrets/dim-bridge-token'),
    '--mount', `type=bind,source=${process.cwd()},target=/workspace,readonly`,
    '--env', 'DIM_ENDPOINT=/run/fullish-agent/rpc.sock',
    '--env', 'DIM_WORKSPACE_ROOT=/var/lib/fullish-agent-work',
    '--env', 'DIM_BRIDGE_SOCKET=/run/fullish-agent/bridge.sock',
    '--env', 'DIM_BRIDGE_TOKEN_FILE=/run/secrets/dim-bridge-token',
    botImage, 'python', '/workspace/scripts/probe_dim_plugin.py', '--endpoint', '/run/fullish-agent/rpc.sock');
  console.log(output);
  const report = JSON.parse(output.trim().split('\n').at(-1));
  assert.equal(report.execution, 'dim-plugin');
  assert.equal(report.sandbox_calls, 0);
  assert.equal(report.confirmed_delivery_and_history, true);
  assert.equal(report.deployment_pre_and_postflight, true);
  assert.equal(pixels.size, 2);
  assert.ok(pixels.has(report.final_png_sha256));
  assert.equal(requests, 5, 'only one text and four image-loop requests; probes use no model');
  console.log(JSON.stringify({ direct_dim_renderer: true, broker_mounted: false,
    model_png_inputs: pixels.size, confirmed_history: true, paid_model_calls: 0 }));
} catch (error) {
  console.error(await docker('logs', prefix).catch(() => 'companion log unavailable'));
  throw error;
} finally {
  await docker('rm', '--force', prefix + '-client').catch(() => {});
  await docker('rm', '--force', prefix).catch(() => {});
  for (const volume of volumes) await docker('volume', 'rm', volume).catch(() => {});
  model.closeAllConnections();
  await new Promise(resolve => model.close(resolve));
  assert.ok(root.startsWith(resolve(tmpdir(), 'fullish-direct-ci-')));
  await chmod(root, 0o700);
  await rm(root, { recursive: true, force: true });
}
