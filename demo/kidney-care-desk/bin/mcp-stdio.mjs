#!/usr/bin/env node
// Expose one of the desk's MCP servers over stdio (newline-delimited JSON-RPC),
// so any MCP host (Claude Desktop, OpenClaw, an IDE...) can use it.
//   node bin/mcp-stdio.mjs fhir
import readline from 'node:readline';
import { allServers } from '../backend/mcp/servers.mjs';
import { Cortex } from '../backend/memory/cortex.mjs';
import { PatientChannel } from '../backend/channel.mjs';

const name = process.argv[2];
const servers = allServers({ cortex: new Cortex(), channel: new PatientChannel() });
const srv = servers.find((s) => s.name === name);
if (!srv) {
  console.error(`usage: mcp-stdio.mjs <${servers.map((s) => s.name).join('|')}>`);
  process.exit(1);
}
const rl = readline.createInterface({ input: process.stdin });
rl.on('line', async (line) => {
  if (!line.trim()) return;
  let req;
  try { req = JSON.parse(line); } catch { return process.stdout.write(JSON.stringify({ jsonrpc: '2.0', id: null, error: { code: -32700, message: 'Parse error' } }) + '\n'); }
  const res = await srv.handle(req);
  if (res) process.stdout.write(JSON.stringify(res) + '\n');
});
