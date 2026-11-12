import { definePlugin } from '@arcships/rutis';
import http from 'node:http';
import { readFile } from 'node:fs/promises';

const guideNames = new Set(['overview', 'flowchart', 'data', 'layout']);

async function invoke(call: any, name: string, cancelled: Promise<void>) {
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
  const body = Buffer.from(JSON.stringify({ session_id: call.sessionId, name, arguments: call.arguments }));
  return await new Promise<any>((resolve, reject) => {
    const request = http.request({ method: 'POST', path: '/invoke', socketPath,
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
      { name: 'visualize_render', description: 'Render SVG, Mermaid or HTML offline in this group sandbox. '
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
        try { return await invoke(call, definition.name, cancelled); }
        catch { return { content: 'fullish_bridge_failed', isError: true }; }
      });
    }
  },
});

export default definePlugin({
  name: 'FullishVisualize', inject: ['partRegistry'],
  async apply(ctx: any) {
    await ctx.use('partRegistry').registerDefinition('session', 'FullishVisualizeTools', parts);
  },
});
