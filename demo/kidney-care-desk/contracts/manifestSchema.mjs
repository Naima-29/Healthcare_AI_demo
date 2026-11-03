// CONTRACT 1 - The agent manifest. Every seat is compiled from its job
// description into this shape; the runtime refuses to load anything else.
import { REFUSALS, GATES, SURFACES } from './catalog.mjs';
import { validate } from './validate.mjs';

const ALL_TOOLS = Object.values(SURFACES).flat().concat([
  'a2a.send', 'a2a.endpoint', 'board.create_item', 'board.update_item',
]);

export const ManifestSchema = {
  type: 'object',
  additionalProperties: false,
  required: ['seat', 'name', 'version', 'location', 'model', 'tools', 'surfaces', 'memory', 'refuses', 'gates', 'unit', 'job_description'],
  properties: {
    seat: { type: 'string', pattern: '^[a-z][a-z0-9_]+$' },
    name: { type: 'string', minLength: 2 },
    role: { type: 'string' },
    version: { type: 'integer', minimum: 1 },
    location: { enum: ['patient_edge', 'hospital_edge'] },
    desk: { type: 'string' },
    model: {
      type: 'object', additionalProperties: false, required: ['edge'],
      properties: { edge: { type: 'string' }, fallback: { type: ['string', 'null'] } },
    },
    humanized: {
      type: 'object',
      properties: { audio: { type: 'string' }, video: { type: 'string' }, text: { type: 'string' }, fuzzy: { type: 'string' } },
    },
    skills: { type: 'array', items: { type: 'string' } },
    tools: { type: 'array', items: { enum: ALL_TOOLS } },
    surfaces: { type: 'array', items: { enum: Object.keys(SURFACES) } },
    memory: {
      type: 'object', additionalProperties: false, required: ['read', 'write'],
      properties: {
        read: { type: 'array', items: { enum: [0, 1, 2, 3] } },
        write: { type: 'array', items: { enum: [0, 1, 2, 3] } },
        partition: { type: 'string' },
      },
    },
    refuses: { type: 'array', items: { enum: Object.keys(REFUSALS) } },
    safety_script: { type: 'string' },
    gates: { type: 'array', minItems: 1, items: { enum: Object.keys(GATES) } },
    a2a_peers: { type: 'array', items: { type: 'string' } },
    artifact: { type: 'string' },
    unit: {
      type: 'object', additionalProperties: false, required: ['kind', 'tc'],
      properties: { kind: { type: 'string' }, tc: { type: 'number', minimum: 0 } },
    },
    job_description: { type: 'string', minLength: 20 },
    compiled_by: { type: 'string' },
    compiled_at: { type: 'string' },
  },
};

export function validateManifest(m) {
  const r = validate(ManifestSchema, m);
  // Semantic checks beyond shape
  if (r.ok) {
    for (const t of m.tools) {
      const surface = Object.entries(SURFACES).find(([, tools]) => tools.includes(t));
      if (surface && !m.surfaces.includes(surface[0])) r.errors.push(`$.tools: ${t} needs surface "${surface[0]}"`);
    }
    for (const w of m.memory.write) if (!m.memory.read.includes(w)) r.errors.push(`$.memory.write: cannot write layer L${w} without reading it`);
    if (m.memory.write.includes(0) || m.memory.write.includes(1)) r.errors.push('$.memory.write: L0/L1 are licensed knowledge, read-only for seats');
    r.ok = r.errors.length === 0;
  }
  return r;
}
