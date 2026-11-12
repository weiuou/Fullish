import { definePlugin } from '@arcships/rutis';
import http from 'node:http';
import { readFile } from 'node:fs/promises';
import { renderPng, validateRender, probeRenderer } from './render-service.mjs';

const guideNames = new Set(['overview', 'flowchart', 'data', 'layout']);

async function invoke(call: any, name: string, cancelled: Promise<void>, args = call.arguments) {
  const socketPath = process.env.FULLISH_AGENT_BRIDGE_SOCKET;
  const localUrl = process.env.FULLISH_AGENT_BRIDGE_URL;
  const token = process.env.FULLISH_AGENT_BRIDGE_TOKEN;
  if (!token || token.length < 32 || (!socketPath && !localUrl)) throw Error('bridge_not_configured');
  let port: number | undefined;
  if (localUrl) {
    const url = new URL(localUrl);
    if (url.protocol !== 'http:' || url.hostname !== '127.0.0.1' || !url.port
        || url.username || url.password || url.search || url.hash || url.pathname !== '/') {
      throw Error('bridge_must_be_private');
    }
    port = Number(url.port);
  }
  const body = Buffer.from(JSON.stringify({ session_id: call.sessionId, name, arguments: args }));
  return await new Promise<any>((resolve, reject) => {
    const request = http.request({ method: 'POST',
      path: name === 'visualize_complete' ? '/render-complete' : '/invoke', socketPath,
      ...(socketPath ? {} : { hostname: '127.0.0.1', port }),
      headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json',
        'Content-Length': body.length }, timeout: 35_000 }, response => {
      const chunks: Buffer[] = [];
      let length = 0;
      response.on('data', chunk => {
        length += chunk.length;
        if (length > 14 * 1024 * 1024) request.destroy(Error('bridge_response_limit'));
        else chunks.push(chunk);
      });
      response.on('end', () => {
        try {
          if (response.statusCode !== 200) throw Error('bridge_rejected');
          resolve(JSON.parse(Buffer.concat(chunks).toString('utf8')));
        } catch { reject(Error('bridge_invalid_response')); }
      });
      response.on('error', reject);
    });
    request.on('error', reject);
    request.on('timeout', () => request.destroy(Error('bridge_timeout')));
    cancelled.then(() => request.destroy(Error('bridge_cancelled'))).catch(() => {});
    request.end(body);
  });
}

const parts = definePlugin({
  name: 'FullishVisualizeTools', inject: ['toolRegistry'],
  async apply(ctx: any) {
    const registry = ctx.use('toolRegistry');
    await registry.register({ name: 'visualize_guide', source: 'extension', risk: 'session',
      description: 'Read one drawing guide before creating or revising a diagram.',
      inputSchema: { type: 'object', properties: { topic: { type: 'string',
        enum: [...guideNames] } }, required: ['topic'], additionalProperties: false } },
    async (call: any) => {
      if (!guideNames.has(call.arguments.topic)) return { content: 'unknown_guide', isError: true };
      const text = await readFile(new URL(`./guides/${call.arguments.topic}.md`, import.meta.url), 'utf8');
      return { content: text };
    });
    for (const definition of [
      { name: 'visualize_render', description: 'Render SVG, Mermaid or HTML directly in the Dim plugin. '
        + 'Returns real pixels for inspection and an artifact ID; at most two renders per task.',
        parameters: { type: 'object', properties: {
          format: { type: 'string', enum: ['svg', 'mermaid', 'html'] },
          source: { type: 'string', maxLength: 48000 },
          width: { type: 'integer', minimum: 240, maximum: 2400 },
          height: { type: 'integer', minimum: 160, maximum: 2400 },
        }, required: ['format', 'source'], additionalProperties: false } },
      { name: 'fullish_reply', description: 'Register the final QQ reply candidate after inspecting the result. '
        + 'Supply artifact_id for an image and text for its caption, or text alone; empty text means silence. '
        + 'This does not send QQ. End the turn after this tool.',
        parameters: { type: 'object', properties: {
          text: { type: 'string', maxLength: 1500 }, artifact_id: { type: 'string', maxLength: 64 },
        }, required: ['text'], additionalProperties: false } },
    ]) {
      await registry.register({ name: definition.name, description: definition.description,
        source: 'extension', risk: definition.name === 'visualize_render' ? 'execute_process' : 'session',
        inputSchema: definition.parameters },
      async (call: any, cancelled: Promise<void>) => {
        if (definition.name !== 'visualize_render') {
          try { return await invoke(call, definition.name, cancelled); }
          catch { return { content: 'fullish_bridge_failed', isError: true }; }
        }
        let ticket: string | undefined;
        try {
          const args = validateRender(call.arguments);
          const grant = await invoke(call, 'visualize_begin', cancelled, {
            format: args.format, width: args.width, height: args.height,
            source_bytes: Buffer.byteLength(args.source),
          });
          if (grant.isError) return grant;
          if (grant.protocol !== 2 || !/^[a-f0-9]{32}$/.test(grant.ticket)) throw Error('bridge_version');
          ticket = grant.ticket;
          const png = await renderPng(args, cancelled, async () => {
            const check = await invoke(call, 'visualize_check', cancelled, { ticket });
            if (check.isError || check.protocol !== 2) throw Error('bridge_rejected');
          });
          const result = await invoke(call, 'visualize_complete', cancelled,
            { ticket, png: png.toString('base64') });
          if (result.isError) return result;
          return { ...result, content: [...result.content, { type: 'image', source: {
            type: 'base64', mediaType: 'image/png', data: png.toString('base64'),
          } }] };
        } catch (error: any) {
          const known = new Set(['dim_invalid_render', 'dim_render_failed', 'dim_render_cancelled', 'dim_render_timeout',
            'dim_render_canvas_content_clipped', 'dim_render_external_resource_rejected',
            'dim_render_chinese_font_missing', 'dim_render_renderer_dependency_failed',
            'dim_render_image_too_large', 'dim_render_source_too_large', 'dim_render_canvas_limit',
            'dim_render_cleanup_failed']);
          const code = known.has(error?.message) ? error.message : 'dim_render_failed';
          if (ticket) {
            try { return await invoke(call, 'visualize_failed', cancelled, { ticket, error: code }); }
            catch { /* Revoked or cancelled tasks cannot submit another result. */ }
          }
          return { content: [{ type: 'text', text: code }], isError: true };
        }
      });
    }
  },
});

export default definePlugin({
  name: 'FullishVisualize', inject: ['partRegistry', 'managementMethods'],
  async apply(ctx: any) {
    await ctx.use('partRegistry').registerDefinition('session', 'FullishVisualizeTools', parts);
    await ctx.use('managementMethods').register('visualize', [
      { name: 'visualize.probe', mutating: false, interruption: 'report', progress: false },
    ], async (call: any, cancelled: Promise<void>) => {
      if (!call.params || typeof call.params !== 'object' || Array.isArray(call.params)
          || Object.keys(call.params).length) return { code: 'invalid_params', message: 'Empty params required' };
      try { return { kind: 'value', value: await probeRenderer(cancelled) }; }
      catch { return { code: 'visualize_probe_failed', message: 'Direct renderer probe failed' }; }
    });
  },
});
