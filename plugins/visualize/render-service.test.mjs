import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readdir } from 'node:fs/promises';
import { tmpdir } from 'node:os';
const { renderPng, probeRenderer } = await import(process.env.FULLISH_RENDER_MODULE || './render-service.mjs');
const args = { format: 'svg', width: 1000, height: 650,
  source: '<svg xmlns="http://www.w3.org/2000/svg" width="1000" height="650" viewBox="0 0 1000 650"><rect width="1000" height="650" fill="#e0f2fe"/><text x="40" y="100">中文直接绘图</text></svg>' };
const directories = async () => new Set((await readdir(tmpdir())).filter(name => name.startsWith('fullish-visualize-')));

test('direct browser renders bounded PNGs and rejects unsafe or overflowing source', async () => {
  const before = await directories();
  const result = await probeRenderer(new Promise(() => {}));
  assert.equal(result.execution, 'dim-plugin');
  assert.equal(result.cases.filter(item => item.outcome === 'png').length, 3);
  assert.equal(result.cases.filter(item => item.outcome === 'rejected').length, 3);
  assert.deepEqual(await directories(), before, 'all private source/PNG directories are reclaimed');
});

test('cancelled direct rendering settles its browser and leaves the queue usable', async () => {
  const before = await directories();
  await assert.rejects(renderPng(args, new Promise(resolve => setTimeout(resolve, 50))), /dim_render_cancelled/);
  await assert.rejects(renderPng(args, Promise.resolve()), /dim_render_cancelled/);
  const png = await renderPng(args);
  assert.equal(png.subarray(0, 8).toString('hex'), '89504e470d0a1a0a');
  assert.deepEqual(await directories(), before);
});
