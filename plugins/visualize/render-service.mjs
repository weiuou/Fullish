/** Local rendering owned by the rutis plugin, with no Fullish sandbox/broker dependency. */
import { mkdtemp, readFile, writeFile, rm, realpath } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { basename, dirname, resolve } from 'node:path';
import { render } from './renderer/render.mjs';

const formats = new Set(['svg', 'mermaid', 'html']);
const failures = new Set(['canvas_content_clipped', 'external_resource_rejected',
  'chinese_font_missing', 'renderer_dependency_failed', 'image_too_large',
  'source_too_large', 'render_cancelled', 'canvas_limit']);
let serial = Promise.resolve();

export function safeRenderError(error, signal) {
  if (signal?.aborted) return 'dim_render_cancelled';
  return failures.has(error?.message) ? 'dim_render_' + error.message : 'dim_render_failed';
}

export function validateRender(args) {
  const { format, source, width = 1200, height = 800 } = args;
  if (Object.keys(args).some(key => !['format', 'source', 'width', 'height'].includes(key))
      || !formats.has(format) || typeof source !== 'string' || !source.trim()
      || Buffer.byteLength(source) > 48000 || !Number.isInteger(width) || !Number.isInteger(height)
      || width < 240 || width > 2400 || height < 160 || height > 2400
      || width * height > 4000000) throw Error('dim_invalid_render');
  return { format, source, width, height };
}

export function renderPng(args, cancelled = new Promise(() => {}), authorize = async () => {}) {
  const options = validateRender(args);
  const controller = new AbortController();
  let started = false, rejectQueued;
  const queuedCancellation = new Promise((_resolve, reject) => { rejectQueued = reject; });
  const cancel = () => {
    controller.abort();
    if (!started) rejectQueued(Error('dim_render_cancelled'));
  };
  cancelled.then(cancel).catch(cancel);
  const job = serial.then(async () => {
    started = true;
    if (controller.signal.aborted) throw Error('dim_render_cancelled');
    await authorize();
    if (controller.signal.aborted) throw Error('dim_render_cancelled');
    const parent = await realpath(tmpdir());
    const root = await mkdtemp(resolve(parent, 'fullish-visualize-'));
    let timedOut = false;
    const timer = setTimeout(() => { timedOut = true; controller.abort(); }, 25000);
    try {
      const input = resolve(root, 'source.txt'), output = resolve(root, 'result.png');
      await writeFile(input, options.source, { flag: 'wx', mode: 0o600 });
      // This trusted runtime choice is never a model argument. The dedicated Dim
      // container retains its OS limits; Chromium runs as its unprivileged user.
      await render({ ...options, root, input, output, signal: controller.signal,
        chromiumSandbox: process.platform !== 'linux' });
      if (controller.signal.aborted) throw Error('render_cancelled');
      const png = await readFile(output);
      if (png.length > 10 * 1024 * 1024 || png.subarray(0, 8).toString('hex') !== '89504e470d0a1a0a'
          || png.readUInt32BE(16) !== options.width || png.readUInt32BE(20) !== options.height) {
        throw Error('image_too_large');
      }
      return png;
    } catch (error) {
      throw Error(timedOut ? 'dim_render_timeout' : safeRenderError(error, controller.signal));
    } finally {
      clearTimeout(timer);
      if (dirname(root) !== parent || !basename(root).startsWith('fullish-visualize-')) {
        throw Error('dim_render_cleanup_failed');
      }
      await rm(root, { recursive: true, force: true });
    }
  });
  serial = job.then(() => {}, () => {});
  return Promise.race([job, queuedCancellation]);
}

export async function probeRenderer(cancelled, authorize = async () => {}) {
  const cases = [
    ['svg', '<svg xmlns="http://www.w3.org/2000/svg" width="1000" height="650" viewBox="0 0 1000 650"><rect width="1000" height="650" fill="#e0f2fe"/><text x="40" y="100" font-size="32">中文完整画布</text></svg>'],
    ['mermaid', 'flowchart TD\n A["输入"] --> B["理解"] --> C["规划"] --> D["绘图"] --> E["检查"] --> F["修改"] --> G["确认"]\n G -->|继续| B\n G -->|结束| H["完成"]'],
    ['html', '<h1>Dim 直接绘图</h1><p style="font-size:28px">中文浏览器验收</p>'],
    ['html', '<img src="https://example.invalid/blocked.png">', 'dim_render_external_resource_rejected'],
    ['html', '<img src="file:///etc/passwd">', 'dim_render_external_resource_rejected'],
    ['html', '<div style="height:2000px">溢出</div>', 'dim_render_canvas_content_clipped'],
  ];
  const results = [];
  for (const [format, source, expected] of cases) {
    try {
      const png = await renderPng({ format, source, width: 1000, height: 650 }, cancelled, authorize);
      if (expected) throw Error('dim_probe_unsafe_source_accepted');
      results.push({ format, outcome: 'png', bytes: png.length, width: 1000, height: 650 });
    } catch (error) {
      if (!expected || error.message !== expected) throw error;
      results.push({ format, outcome: 'rejected', error: expected });
    }
  }
  return { protocol: 2, execution: 'dim-plugin', cases: results };
}
