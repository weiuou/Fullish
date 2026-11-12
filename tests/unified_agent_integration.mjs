/** Real one-container native/rutis/browser flow and Linux UID boundary evidence. */
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { createHash, randomUUID } from 'node:crypto';
import { mkdtemp, writeFile, chown, chmod, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { resolve } from 'node:path';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';

assert.equal(process.platform, 'linux');
assert.equal(process.env.FULLISH_UNIFIED_CI, '1');
const execute = promisify(execFile);
const docker = async (...args) => (await execute('docker', args,
  { timeout: 300000, maxBuffer: 8 * 1024 * 1024 })).stdout;
const root = await mkdtemp(resolve(tmpdir(), 'fullish-unified-ci-'));
const prefix = 'fullish-unified-' + randomUUID().replaceAll('-', '').slice(0, 12);
const image = process.argv[2] || 'fullish:ci';
const targets = ['/run/fullish-integrated', '/var/lib/fullish-integrated-agent',
  '/var/lib/fullish-integrated-work', '/var/lib/fullish-diagnostics', '/var/lib/fullish/control'];
const volumes = targets.map((_, index) => prefix + '-' + index);
let requests = 0, selfChecked = false, sourceRead = false;
const pixels = new Set();
const server = createServer(async (req, res) => {
  try {
    assert.equal(req.url, '/v1/chat/completions');
    assert.equal(req.headers.authorization, 'Bearer synthetic-unified-key');
    let text = '';
    for await (const chunk of req) text += chunk;
    const input = JSON.parse(text);
    requests++;
    const offered = (input.tools || []).map(tool => tool.function.name);
    const done = input.messages.filter(item => item.role === 'tool').map(item => item.tool_call_id);
    let message;
    if (!offered.length) message = { role: 'assistant', content: '本地 Dim 协议验收成功。' };
    else {
      assert.deepEqual(new Set(offered), new Set(['fullish_inspect', 'fullish_self_check',
        'visualize_guide', 'visualize_render', 'fullish_reply']));
      const material = JSON.stringify(input);
      assert.ok(!material.includes('synthetic-fullish-secret'));
      let name, id, args;
      if (done.includes('fixture-final')) message = { role: 'assistant', content: 'Unused model tail.' };
      else {
        if (!done.includes('fixture-check')) {
          name = 'fullish_self_check'; id = 'fixture-check'; args = {};
        } else if (!done.includes('fixture-inspect')) {
          const value = JSON.parse(input.messages.find(item => item.tool_call_id === 'fixture-check').content);
          assert.equal(value.boundary_verified, true);
          assert.equal(value.fullish_uid, 10001);
          assert.equal(value.dim_uid, 10002);
          assert.equal(value.fullish_secret_in_environment, false);
          assert.ok(JSON.stringify(value.diagnostics).includes('TimeoutError'));
          selfChecked = true;
          name = 'fullish_inspect'; id = 'fixture-inspect';
          args = { action: 'read', path: 'src/fullish/notifications/deployment.py', start_line: 140, line_count: 100 };
        } else {
          assert.ok(material.includes('asyncio.timeout(180)'));
          sourceRead = true;
          const count = done.filter(item => item.startsWith('fixture-render-')).length;
          if (count < 2) {
            name = 'visualize_render'; id = 'fixture-render-' + (count + 1);
            args = { format: 'svg', width: 1000, height: 650,
              source: '<svg xmlns="http://www.w3.org/2000/svg" width="1000" height="650" viewBox="0 0 1000 650">'
                + `<rect width="1000" height="650" fill="${count ? '#ccfbf1' : '#e0f2fe'}"/>`
                + '<text x="40" y="100" font-size="32">同容器 → 只读自检 → 插件绘图 → QQ 确认</text></svg>' };
          } else {
            const images = [...material.matchAll(/data:image\/png;base64,([A-Za-z0-9+/=]+)/g)];
            for (const image of images) pixels.add(createHash('sha256').update(Buffer.from(image[1], 'base64')).digest('hex'));
            assert.equal(pixels.size, 2);
            name = 'fullish_reply'; id = 'fixture-final';
            args = { text: '插件直接绘图验收成功。', artifact_id: [...material.matchAll(/img_[a-f0-9]{32}/g)].at(-1)[0] };
          }
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
  } catch (error) { console.error(error); res.writeHead(500); res.end('fixture assertion failed'); }
});
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
try {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  for (const [name, value, uid] of [['model-key', 'synthetic-unified-key', 10002],
    ['bridge-fullish', 'synthetic-unified-bridge-token-123456789', 10001],
    ['bridge-dim', 'synthetic-unified-bridge-token-123456789', 10002]]) {
    const path = resolve(root, name);
    await writeFile(path, value, { mode: 0o600 });
    await chown(path, uid, 10003);
  }
  const mount = (name, path) => ['--mount', `type=bind,source=${resolve(root, name)},target=${path},readonly`];
  const common = ['--init', '--network', 'host', '--read-only', '--user', '0:0', '--no-healthcheck',
    '--tmpfs', '/tmp:rw,noexec,nosuid,size=128m', '--cap-drop', 'ALL', '--cap-add', 'SETUID', '--cap-add', 'SETGID',
    '--security-opt', 'no-new-privileges:true', '--pids-limit', '256', '--memory', '1g', '--memory-swap', '1g', '--cpus', '1',
    ...volumes.flatMap((volume, i) => ['--volume', volume + ':' + targets[i]]),
    ...mount('model-key', '/run/secrets/dim-model-key'), ...mount('bridge-fullish', '/run/secrets/dim-bridge-token'),
    ...mount('bridge-dim', '/run/secrets/dim-bridge-token-agent'),
    '--mount', `type=bind,source=${process.cwd()},target=/workspace,readonly`,
    '--env', 'FULLISH_UNIFIED_CI=1', '--env', 'DIM_DEPLOYMENT=unified', '--env', 'DIM_INSPECTION=true',
    '--env', 'DIM_VISUALIZATION=true', '--env', 'AGENT_BACKEND=dim', '--env', 'DIM_MODEL_ID=fixture',
    '--env', `DIM_MODEL_BASE_URL=http://127.0.0.1:${server.address().port}/v1`,
    '--env', 'DIM_ALLOWED_GROUPS=gggggggggggggggg', '--env', 'GROUP_CONTROL_DIR=/var/lib/fullish/control',
    '--env', 'FULLISH_DIAGNOSTICS_DIR=/var/lib/fullish-diagnostics', '--env', 'QQ_APP_SECRET=synthetic-fullish-secret',
    '--entrypoint', 'python'];
  let completed;
  const running = docker('run', '--name', prefix, ...common, image,
    '/opt/fullish-integrated/launch.py', '--with-agent', 'python', '/workspace/tests/unified_fixture.py')
    .then(output => ({ output }), error => ({ error })).then(result => { completed = result; return result; });
  let ready = false;
  for (let attempt = 0; attempt < 100; attempt++) {
    if (completed?.error) throw completed.error;
    const result = await docker('exec', '--user', '10001:10003', prefix, 'python', '-c',
      "from pathlib import Path; assert Path('/var/lib/fullish/control/boundary-sentinel').exists()")
      .then(() => true, () => false);
    if (result) { ready = true; break; }
    await pause(500);
  }
  assert.ok(ready, 'actual Fullish identity starts inside the one-container runtime');
  const inode = async () => (await docker('exec', '--user', '10002:10003', prefix, 'python', '-c',
    "from pathlib import Path; print(Path('/run/fullish-integrated/dim/rpc.sock').stat().st_ino)")).trim();
  const before = await inode();
  const preflight = await docker('run', '--rm', '--name', prefix + '-preflight', ...common, image,
    '/opt/fullish-integrated/launch.py', 'python', '-m', 'fullish.agents.check', '--preflight');
  assert.ok(preflight.includes('"sandbox_calls": 0'));
  assert.equal(await inode(), before, 'candidate preflight cannot replace the running RPC socket');
  assert.equal(requests, 0, 'preflight never invokes the model');
  console.log(await docker('exec', '--user', '10002:10003', prefix, 'python', '/workspace/tests/unified_boundary.py'));
  await docker('exec', '--user', '10001:10003', prefix, 'python', '-c',
    "from pathlib import Path; Path('/tmp/fullish-fullish/continue-fixture').touch()");
  const result = await running;
  if (result.error) throw result.error;
  console.log(result.output);
  const report = JSON.parse(result.output.trim().split('\n').at(-1));
  assert.ok(report.confirmed_delivery_and_history && report.deployment_pre_and_postflight);
  assert.ok(selfChecked && sourceRead && pixels.has(report.final_png_sha256));
  assert.equal(requests, 7);
  console.log(JSON.stringify({ unified_container: true, kernel_readonly_boundary: true,
    read_only_inspection: true, actual_png_inputs: pixels.size, confirmed_history: true,
    preflight_isolated: true, paid_model_calls: 0, real_qq_calls: 0 }));
} catch (error) {
  console.error(await docker('logs', prefix).catch(() => 'container log unavailable'));
  throw error;
} finally {
  await docker('rm', '--force', prefix + '-preflight').catch(() => {});
  await docker('rm', '--force', prefix).catch(() => {});
  for (const volume of volumes) await docker('volume', 'rm', volume).catch(() => {});
  server.closeAllConnections();
  await new Promise(resolve => server.close(resolve));
  assert.ok(root.startsWith(resolve(tmpdir(), 'fullish-unified-ci-')));
  await chmod(root, 0o700);
  await rm(root, { recursive: true, force: true });
}
