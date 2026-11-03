// Model Context Protocol (MCP) - minimal, spec-shaped implementation.
// JSON-RPC 2.0; methods: initialize, notifications/initialized, tools/list,
// tools/call. Results carry `content` (text) + `structuredContent` + `isError`.
// Each server can be reached three ways:
//   1. in-process (seats call it through McpRegistry.call)
//   2. HTTP   POST /mcp/<server>        (see server.mjs)
//   3. stdio  node bin/mcp-stdio.mjs <server>  (usable by any MCP host)

export const PROTOCOL_VERSION = '2025-06-18';

export class McpServer {
  constructor({ name, version = '1.0.0', description = '' }) {
    this.name = name; this.version = version; this.description = description;
    this.tools = new Map();
  }
  tool(name, { description, inputSchema = { type: 'object', properties: {} } }, handler) {
    this.tools.set(name, { name, description, inputSchema, handler });
    return this;
  }
  async handle(req) {
    const { id, method, params = {} } = req;
    const ok = (result) => ({ jsonrpc: '2.0', id, result });
    const err = (code, message) => ({ jsonrpc: '2.0', id, error: { code, message } });
    try {
      switch (method) {
        case 'initialize':
          return ok({ protocolVersion: PROTOCOL_VERSION, capabilities: { tools: { listChanged: false } }, serverInfo: { name: this.name, version: this.version } });
        case 'notifications/initialized':
          return null;
        case 'ping':
          return ok({});
        case 'tools/list':
          return ok({ tools: [...this.tools.values()].map(({ handler, ...t }) => t) });
        case 'tools/call': {
          const t = this.tools.get(params.name);
          if (!t) return err(-32602, `Unknown tool: ${params.name}`);
          try {
            const out = await t.handler(params.arguments || {});
            return ok({ content: [{ type: 'text', text: JSON.stringify(out) }], structuredContent: out, isError: false });
          } catch (e) {
            return ok({ content: [{ type: 'text', text: String(e.message || e) }], isError: true });
          }
        }
        default:
          return err(-32601, `Method not found: ${method}`);
      }
    } catch (e) {
      return err(-32603, String(e.message || e));
    }
  }
}

let rid = 0;
export class McpRegistry {
  constructor() { this.servers = new Map(); this.toolIndex = new Map(); }
  register(server) {
    this.servers.set(server.name, server);
    for (const name of server.tools.keys()) this.toolIndex.set(name, server.name);
    return this;
  }
  serverFor(tool) { return this.toolIndex.get(tool); }
  async call(tool, args) {
    const sname = this.toolIndex.get(tool);
    if (!sname) throw new Error(`No MCP server exposes ${tool}`);
    const res = await this.servers.get(sname).handle({ jsonrpc: '2.0', id: ++rid, method: 'tools/call', params: { name: tool, arguments: args } });
    if (res.error) throw new Error(res.error.message);
    if (res.result.isError) throw new Error(res.result.content?.[0]?.text || 'tool error');
    return res.result.structuredContent;
  }
}
