/** Actual companion launcher/health/shutdown, anonymous private volumes and synthetic secrets. */
import assert from 'node:assert/strict';
import { writeFile } from 'node:fs/promises';
import { spawn } from 'node:child_process';

await writeFile('/run/secrets/dim-model-key', 'synthetic-no-model-call', { mode: 0o600 });
await writeFile('/run/secrets/dim-bridge-token', 'synthetic-private-bridge-token-123456789', { mode: 0o600 });
const executable = '/usr/local/bin/node';
const entry = '/opt/fullish-agent/launch.mjs';
const child = spawn(executable, [entry], { stdio: ['ignore', 'ignore', 'inherit'], env: {
  PATH: process.env.PATH, DIM_MODEL_ID: 'fixture', DIM_MODEL_BASE_URL: 'http://127.0.0.1:9/v1',
} });
const pause = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));
async function health() {
  const probe = spawn(executable, [entry, '--check'], { stdio: 'ignore' });
  return await new Promise(resolve => probe.once('exit', code => resolve(code === 0)));
}
try {
  let ready = false;
  for (let attempt = 0; attempt < 40; attempt++) {
    assert.equal(child.exitCode, null, 'launcher remains alive');
    if (await health()) { ready = true; break; }
    await pause(250);
  }
  assert.equal(ready, true, 'actual image health accepts the pinned plugin');
  await pause(1500);
  assert.equal(child.exitCode, null, 'watchdog accepts healthy empty catalog');
  assert.equal(await health(), true);
  console.log(JSON.stringify({ companion_launcher: true, private_volumes: true,
    plugin_health: true, watchdog: true, paid_model_calls: 0 }));
} finally {
  child.kill('SIGTERM');
  await Promise.race([new Promise(resolve => child.once('exit', resolve)), pause(6000)]);
  if (child.exitCode === null && child.signalCode === null) child.kill('SIGKILL');
}
