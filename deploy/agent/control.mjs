/** Trusted companion control only. Never expose this client as a model tool. */
import { createConnection } from 'node:net';
import { lstat } from 'node:fs/promises';

export async function connectControl(endpoint) {
  const info = await lstat(endpoint);
  const integrated = process.env.DIM_DEPLOYMENT === 'unified';
  if (!info.isSocket() || info.uid !== process.getuid()
      || (integrated ? info.gid !== 10003 || (info.mode & 0o777) !== 0o660 : info.mode & 0o077)) {
    throw Error('private_agent_socket_required');
  }
  const socket = createConnection(endpoint);
  socket.setEncoding('utf8');
  const pending = new Map();
  let sequence = 0, buffered = '';
  const close = () => {
    socket.destroy();
    for (const waiter of pending.values()) waiter.reject(Error('agent_control_closed'));
    pending.clear();
  };
  socket.on('error', close);
  socket.on('close', close);
  socket.on('data', chunk => {
    buffered += chunk;
    if (buffered.length > 1024 * 1024) return close();
    while (buffered.includes('\n')) {
      const end = buffered.indexOf('\n');
      let reply;
      try { reply = JSON.parse(buffered.slice(0, end)); } catch { return close(); }
      buffered = buffered.slice(end + 1);
      const waiter = pending.get(reply.id);
      if (!waiter) continue;
      pending.delete(reply.id);
      if (reply.error) waiter.reject(Error('agent_control_rpc_failed'));
      else waiter.resolve(reply.result);
    }
  });
  const call = (method, params = {}, timeout = 3000) => new Promise((resolve, reject) => {
    if (socket.destroyed) return reject(Error('agent_control_closed'));
    const id = ++sequence;
    const deadline = setTimeout(() => {
      pending.delete(id);
      reject(Error('agent_control_timeout'));
    }, timeout);
    pending.set(id, {
      resolve: result => { clearTimeout(deadline); resolve(result); },
      reject: error => { clearTimeout(deadline); reject(error); },
    });
    socket.write(JSON.stringify({ jsonrpc: '2.0', id, method, params }) + '\n');
  });
  try {
    const hello = await call('hello', { protocolVersion: 3, clientKind: 'other' });
    if (hello.protocolVersion !== 3 || hello.serverVersion !== '0.1.0-alpha.20') {
      throw Error('agent_control_version_mismatch');
    }
    return { call, close };
  } catch (error) { close(); throw error; }
}

export async function stopExpiredSessions(call, now = Date.now()) {
  // Catalog reads do not attach or resume stored sessions. Previews stay off.
  const page = await call('session.catalog', { limit: 128, withPreview: false });
  if (!Array.isArray(page.sessions) || page.nextCursor) throw Error('agent_catalog_limit');
  let stopped = 0;
  for (const session of page.sessions) {
    if (!session.running) continue;
    const age = now - session.createdAt;
    if (!Number.isFinite(age) || age < 0) throw Error('agent_session_clock_invalid');
    if (age < 90_000 && !(session.attachedClients === 0 && age >= 2000)) continue;
    await call('session.stop', { sessionId: session.sessionId, branchId: 'main',
      reason: 'Fullish runtime deadline or controller detached' }, 5000);
    await call('session.idle', { sessionId: session.sessionId, branchId: 'main' }, 5000);
    stopped++;
  }
  return stopped;
}
