import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, writeFile, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { resolve } from 'node:path';
import { render } from './render.mjs';

const browser = process.env.FULLISH_CHROMIUM || '/usr/bin/chromium';
async function withSource(format, source, run) {
  const root = await mkdtemp(resolve(tmpdir(), 'fullish-render-'));
  const input = resolve(root, 'source.txt'), output = resolve(root, 'result.png');
  await writeFile(input, source);
  try { await run({ root, input, output, format, width: 1000, height: 650, browser }); }
  finally { await rm(root, { recursive: true, force: true }); }
}

test('real offline browser renders Chinese SVG, HTML and Mermaid as bounded PNG', async () => {
  const cases = [
    ['svg', '<svg xmlns="http://www.w3.org/2000/svg" width="800" height="500"><rect width="800" height="500" fill="#e0f2fe"/><text x="40" y="100" font-size="32">群聊 → 智能体 → 图片</text></svg>'],
    ['html', '<h1>开发路径</h1><p style="font-size:28px">先验证，再绘图，最后投递。</p><script>while(true){}</script>'],
    ['mermaid', 'flowchart LR\n A["群消息"] --> B["Dim 推理"] --> C["离线绘图"] --> D["QQ 确认"]'],
    ['svg', '<svg xmlns="http://www.w3.org/2000/svg" width="1000" height="650" viewBox="0 0 1000 650"><rect width="1000" height="650" fill="#e0f2fe"/><text x="500" y="325" text-anchor="middle" font-size="32">完整画布</text></svg>'],
    ['mermaid', 'flowchart TD\n A["收到请求"] --> B["检查权限"] --> C["准备上下文"] --> D["推理"] --> E["调用工具"] --> F["生成图片"] --> G["检查结果"] --> H["确认投递"]\n H -->|继续| C\n H -->|结束| I["完成"]'],
  ];
  for (const [format, source] of cases) {
    await withSource(format, source, async options => {
      const result = await render(options);
      const png = await readFile(options.output);
      assert.equal(png.subarray(0, 8).toString('hex'), '89504e470d0a1a0a');
      assert.equal(png.readUInt32BE(16), 1000);
      assert.equal(png.readUInt32BE(20), 650);
      assert.equal(result.bytes, png.length);
      assert.ok(png.length > 1000 && png.length < 10 * 1024 * 1024);
    });
  }
});

test('renderer refuses remote/file subresources and overflowing layouts', async () => {
  for (const source of [
    '<img src="https://example.invalid/private.png">',
    '<style>div{background:url(file:///etc/passwd)}</style><div>hello</div>',
    '<iframe src="data:text/html,hello"></iframe>',
    '<link rel="stylesheet" href="https://example.invalid/theme.css">',
  ]) {
    await withSource('html', source, async options => {
      await assert.rejects(render(options), /external_resource_rejected/);
      await assert.rejects(readFile(options.output), { code: 'ENOENT' });
    });
  }
  await withSource('html', '<div style="height:2000px">超出画布</div>', async options => {
    await assert.rejects(render(options), /canvas_content_clipped/);
  });
});

test('source/output boundaries and size limits apply before starting a browser', async () => {
  await withSource('svg', '<svg/>', async options => {
    await assert.rejects(render({ ...options, width: 99999 }), /canvas_limit/);
    await assert.rejects(render({ ...options, input: resolve(options.root, '..', 'outside.svg') }), /workspace_path_required/);
    await assert.rejects(render({ ...options, format: 'pdf' }), /unsupported_format/);
    await writeFile(options.output, 'existing');
    await assert.rejects(render(options), /output_exists/);
  });
});
