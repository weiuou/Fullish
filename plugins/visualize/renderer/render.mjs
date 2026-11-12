#!/usr/bin/env node
/** Bounded source-to-PNG renderer, invoked directly by the Dim rutis plugin. */
import { chromium } from 'playwright-core';
import { readFile, writeFile, realpath, lstat } from 'node:fs/promises';
import { dirname, isAbsolute, relative, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';

const MAX_SOURCE = 48_000;
const MAX_OUTPUT = 10 * 1024 * 1024;
const MAX_PIXELS = 4_000_000;
const FORMATS = new Set(['svg', 'mermaid', 'html']);

async function workspacePath(root, path, output = false) {
  if (!path || !isAbsolute(path) || path.includes('\0')) throw Error('invalid_path');
  const base = await realpath(root);
  const parent = await realpath(dirname(path));
  const target = resolve(parent, path.split(/[\\/]/).at(-1));
  const inside = relative(base, target);
  if (!inside || inside.startsWith(`..${sep}`) || inside === '..' || isAbsolute(inside)) {
    throw Error('workspace_path_required');
  }
  try {
    const info = await lstat(target);
    if (info.isSymbolicLink() || !info.isFile() || info.nlink !== 1) throw Error('ordinary_file_required');
    if (output) throw Error('output_exists');
    if (info.size > MAX_SOURCE) throw Error('source_too_large');
  } catch (error) {
    if (!output || error.code !== 'ENOENT') throw error;
  }
  return target;
}

export async function render({ root = '/workspace', input, output, format, width = 1200, height = 800,
  browser = process.env.FULLISH_CHROMIUM || '/usr/bin/chromium', chromiumSandbox = true, signal }) {
  if (signal?.aborted) throw Error('render_cancelled');
  if (!FORMATS.has(format)) throw Error('unsupported_format');
  if (!Number.isInteger(width) || !Number.isInteger(height) || width < 240 || height < 160
      || width > 2400 || height > 2400 || width * height > MAX_PIXELS) throw Error('canvas_limit');
  const sourcePath = await workspacePath(root, input);
  const outputPath = await workspacePath(root, output, true);
  const source = await readFile(sourcePath, 'utf8');
  if (!source.trim() || Buffer.byteLength(source) > MAX_SOURCE) throw Error('source_too_large');
  const childEnv = { PATH: process.platform === 'win32' ? process.env.PATH : '/usr/local/bin:/usr/bin:/bin',
    LANG: 'C.UTF-8', HOME: root,
    ...(process.platform === 'win32' ? { SystemRoot: process.env.SystemRoot } : {}) };
  if (process.platform === 'linux') {
    try {
      const { stdout } = await promisify(execFile)('/usr/bin/fc-match',
        ['--format=%{family}', 'Noto Sans CJK SC'], { timeout: 3000, env: childEnv });
      if (!stdout.includes('Noto Sans CJK SC')) throw Error('chinese_font_missing');
    } catch { throw Error('chinese_font_missing'); }
  }
  const instance = await chromium.launch({
    executablePath: browser, headless: true, chromiumSandbox,
    // Browser children never inherit model keys, bridge tokens or the Dim home.
    env: childEnv,
    args: ['--disable-background-networking', '--disable-component-update', '--disable-sync',
      '--disable-gpu', '--in-process-gpu', '--renderer-process-limit=1', '--num-raster-threads=1',
      '--disable-crash-reporter', '--js-flags=--max-old-space-size=96'],
    timeout: 10_000,
  });
  const abort = () => { void instance.close().catch(() => {}); };
  signal?.addEventListener('abort', abort, { once: true });
  try {
    if (signal?.aborted) throw Error('render_cancelled');
    const context = await instance.newContext({ viewport: { width, height }, deviceScaleFactor: 1,
      // Mermaid's sanitizer needs browser callbacks. Only its strict DSL uses a scripted
      // context; arbitrary HTML/SVG rendering keeps page scripts disabled.
      javaScriptEnabled: format === 'mermaid', serviceWorkers: 'block', acceptDownloads: false });
    const page = await context.newPage();
    const blocked = [];
    await context.route('**/*', route => { blocked.push(route.request().url()); return route.abort(); });
    page.on('download', download => download.cancel());
    page.on('popup', popup => popup.close());
    const csp = "default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src 'none'; "
      + "frame-src 'none'; object-src 'none'; connect-src 'none'; base-uri 'none'; form-action 'none'";
    await page.setContent(`<!doctype html><html><head><meta charset="utf-8">
      <meta http-equiv="Content-Security-Policy" content="${csp}">
      <style>html,body{margin:0;background:#f8fafc;color:#172033;font-family:"Noto Sans CJK SC",sans-serif}
      *{box-sizing:border-box}#canvas{width:${width}px;height:${height}px;overflow:hidden;padding:24px}
      #canvas>svg{display:block;max-width:100%;max-height:100%}</style></head><body><main id="canvas"></main></body></html>`);
    if (format === 'mermaid') {
      const location = dirname(fileURLToPath(import.meta.url));
      const bundle = await readFile(resolve(location, 'node_modules/mermaid/dist/mermaid.min.js'), 'utf8');
      // DevTools evaluation executes only our pinned renderer code; page-authored scripts stay off.
      // Execute the trusted IIFE as a global script (strict eval in page.evaluate would
      // hide Mermaid's bundled var from its globalThis export).
      const devtools = await context.newCDPSession(page);
      const loaded = await devtools.send('Runtime.evaluate', { expression: bundle });
      if (loaded.exceptionDetails) throw Error('renderer_dependency_failed');
      await devtools.detach();
      await page.evaluate(async text => {
        globalThis.mermaid.initialize({ startOnLoad: false, securityLevel: 'strict', theme: 'neutral',
          fontFamily: 'Noto Sans CJK SC, sans-serif', maxTextSize: 48_000,
          flowchart: { htmlLabels: false }, suppressErrorRendering: true });
        const { svg } = await globalThis.mermaid.render('fullish_diagram', text);
        document.getElementById('canvas').innerHTML = svg;
      }, source);
    } else {
      await page.evaluate(markup => { document.getElementById('canvas').innerHTML = markup; }, source);
    }
    await page.evaluate(async () => { await document.fonts.ready; });
    // Remote resources are never required for a successful render. Reject instead of silently
    // delivering a screenshot with a missing image/font.
    const external = await page.evaluate(() => {
      const nodes = [...document.querySelectorAll('[src],[href],[xlink\\:href],iframe,object,embed,link,base')];
      const badNodes = nodes.some(node => {
        const url = node.getAttribute('src') || node.getAttribute('href') || node.getAttribute('xlink:href');
        return ['IFRAME', 'OBJECT', 'EMBED', 'LINK', 'BASE'].includes(node.tagName.toUpperCase())
          || (url && !url.startsWith('data:image/') && !url.startsWith('#'));
      });
      const css = [...document.querySelectorAll('style,[style]')].map(node =>
        node.tagName === 'STYLE' ? node.textContent : node.getAttribute('style')).join('\n');
      const urls = [...css.matchAll(/url\(\s*["']?([^\s"')]+)/gi)].map(match => match[1]);
      return badNodes || /@import\b/i.test(css)
        || urls.some(url => !url.startsWith('data:image/') && !url.startsWith('#'));
    });
    if (blocked.length || external) throw Error('external_resource_rejected');
    const clipped = await page.evaluate(() => {
      const canvas = document.getElementById('canvas');
      return canvas.scrollHeight > canvas.clientHeight || canvas.scrollWidth > canvas.clientWidth;
    });
    if (clipped) throw Error('canvas_content_clipped');
    const image = await page.screenshot({ type: 'png', animations: 'disabled', timeout: 10_000 });
    if (image.length > MAX_OUTPUT) throw Error('image_too_large');
    await writeFile(outputPath, image, { flag: 'wx', mode: 0o600 });
    return { format: 'png', bytes: image.length, width, height };
  } finally {
    signal?.removeEventListener('abort', abort);
    await instance.close();
  }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const [format, input, output, width = '1200', height = '800'] = process.argv.slice(2);
  let deadline = setTimeout(() => process.exit(124), 25_000);
  try {
    const result = await render({ format, input, output, width: Number(width), height: Number(height) });
    process.stdout.write(JSON.stringify(result) + '\n');
  } catch (error) {
    // Browser errors can contain source and URLs. Expose only fixed codes.
    const allowed = /^[a-z_]{1,64}$/;
    process.stderr.write(JSON.stringify({ error: allowed.test(error.message) ? error.message : 'render_failed' }) + '\n');
    process.exitCode = 1;
  } finally {
    clearTimeout(deadline);
  }
}
