/** Read-only source and diagnostic views. Never opens production config or executes commands. */
import { readFile, readdir, realpath, lstat, access } from 'node:fs/promises';
import { constants } from 'node:fs';
import { resolve, relative, isAbsolute, sep } from 'node:path';
import { probeRenderer } from './render-service.mjs';

const sourceRoot = '/opt/fullish-readonly';
const diagnosticsRoot = '/var/lib/fullish-diagnostics';
const suffix = /\.(py|md|toml|json|yaml|yml)$/;

export async function inspectSource(args) {
  const { action, path = '', start_line = 1, line_count = 120 } = args;
  if (Object.keys(args).some(key => !['action', 'path', 'start_line', 'line_count'].includes(key))
      || !['list', 'read'].includes(action) || typeof path !== 'string' || path.length > 240
      || path.includes('\\') || path.includes('\0') || isAbsolute(path)
      || path.split('/').some(part => part === '..' || part.startsWith('.'))
      || !Number.isInteger(start_line) || start_line < 1 || start_line > 20000
      || !Number.isInteger(line_count) || line_count < 1 || line_count > 120) {
    throw Error('fullish_invalid_inspection');
  }
  const base = await realpath(sourceRoot);
  const target = await realpath(resolve(base, path));
  const inside = relative(base, target);
  if (isAbsolute(inside) || inside === '..' || inside.startsWith('..' + sep)) throw Error('fullish_path_denied');
  const info = await lstat(target);
  if (info.isSymbolicLink()) throw Error('fullish_path_denied');
  if (action === 'list') {
    if (!info.isDirectory()) throw Error('fullish_path_denied');
    const items = await readdir(target, { withFileTypes: true });
    return { path, entries: items.filter(item => !item.name.startsWith('.') && !item.isSymbolicLink())
      .slice(0, 100).map(item => ({ name: item.name, directory: item.isDirectory() })) };
  }
  if (!info.isFile() || info.nlink !== 1 || info.size > 512000 || !suffix.test(path)) throw Error('fullish_path_denied');
  const lines = (await readFile(target, 'utf8')).split('\n');
  const content = lines.slice(start_line - 1, start_line - 1 + line_count).join('\n');
  if (Buffer.byteLength(content) > 12000) throw Error('fullish_read_limit');
  return { path, start_line, total_lines: lines.length, content };
}

const writable = async path => { try { await access(path, constants.W_OK); return true; } catch { return false; } };
export async function selfCheck(grant, render, cancelled) {
  let diagnostics;
  try {
    const path = resolve(diagnosticsRoot, 'events.json');
    const info = await lstat(path);
    if (!info.isFile() || info.nlink !== 1 || info.size > 65536 || info.uid !== 10001
        || (info.mode & 0o022)) throw Error('invalid_diagnostics');
    diagnostics = JSON.parse(await readFile(path, 'utf8'));
    if (!Array.isArray(diagnostics)) throw Error('invalid_diagnostics');
  } catch { diagnostics = { available: false }; }
  const source = await inspectSource({ action: 'read', path: 'src/fullish/notifications/deployment.py', line_count: 1 });
  const fullishWritable = await writable(sourceRoot) || await writable(diagnosticsRoot)
    || await writable('/var/lib/fullish/control') || await writable('/app/.venv');
  const result = { ...grant, dim_uid: process.getuid(), source_readable: Boolean(source.content),
    fullish_writable: fullishWritable, diagnostics,
    boundary_verified: grant.deployment === 'unified' && grant.fullish_uid === 10001
      && process.getuid() === 10002 && !fullishWritable };
  if (render) result.renderer = await probeRenderer(cancelled);
  return result;
}
