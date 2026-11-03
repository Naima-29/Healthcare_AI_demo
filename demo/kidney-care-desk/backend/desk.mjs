// Assembles the Kidney Care Desk: keys, bus, MCP registry, Cortex, Board,
// runtime and the eight seats + Chief of Staff. Used by the HTTP server, the
// CLI golden-path runner and the tests.
import { KeyRing } from '../contracts/a2aSchema.mjs';
import { Bus } from './runtime/bus.mjs';
import { Board } from './runtime/board.mjs';
import { Cases } from './runtime/cases.mjs';
import { Runtime } from './runtime/runtime.mjs';
import { McpRegistry } from './mcp/protocol.mjs';
import { allServers } from './mcp/servers.mjs';
import { Cortex } from './memory/cortex.mjs';
import { PatientChannel } from './channel.mjs';
import { Companion } from './seats/companion.mjs';
import { Dispatcher } from './seats/dispatcher.mjs';
import { ImagingReader, GenomicsInterpreter, RenalGuardian, SafetyVerifier } from './seats/clinical.mjs';
import { CaseBriefer, Archivist } from './seats/briefer.mjs';
import { ChiefOfStaff } from './seats/chiefOfStaff.mjs';
import { CONTENT_CLASSES } from './runtime/guard.mjs';
import { hub, uid } from './core/hub.mjs';

export function createDesk({ cortexDir } = {}) {
  const ctx = {};
  ctx.keyring = new KeyRing();
  ctx.bus = new Bus(ctx.keyring);
  ctx.board = new Board(ctx.keyring);
  ctx.cases = new Cases();
  ctx.cortex = new Cortex(cortexDir);
  ctx.channel = new PatientChannel();
  ctx.mcp = new McpRegistry();
  for (const s of allServers({ cortex: ctx.cortex, channel: ctx.channel })) ctx.mcp.register(s);
  ctx.runtime = new Runtime(ctx);
  ctx.runtime.loadSeedManifests();
  const seats = [new Companion(ctx), new Dispatcher(ctx), new ImagingReader(ctx), new GenomicsInterpreter(ctx), new RenalGuardian(ctx), new SafetyVerifier(ctx), new CaseBriefer(ctx), new Archivist(ctx)];
  for (const s of seats) { ctx.keyring.ensure(s.id); ctx.runtime.attach(s); }
  ctx.chief = new ChiefOfStaff(ctx);
  ctx.channel.onInbound = (patient, text) => ctx.runtime.seat('companion').onPatientText(patient, text);

  // ---------------- the three human doors --------------------------------
  ctx.doors = {
    director: (text) => ctx.chief.handle(text),
    patient: {
      say: (patient, text, features) => ctx.runtime.seat('companion').onPatientText(patient, text, features),
      attachLab: async (patient) => {
        const { ocr_text } = await ctx.mcp.call('lab.latest', { patient });
        return ctx.runtime.seat('companion').attachLabPhoto(patient, ocr_text);
      },
    },
    clinician: {
      acknowledge: (itemId, clinician, role = 'oncall_clinician') => {
        const act = ctx.board.humanAct(itemId, { action: 'acknowledge', clinician, role });
        ctx.runtime.seat('dispatcher').acknowledged(itemId);
        return act;
      },
      review: (itemId, clinician, role) => ctx.board.humanAct(itemId, { action: 'review', clinician, role }),
      confirmRule: (itemId, clinician, confirm = true) => {
        const item = ctx.board.get(itemId);
        const act = ctx.board.humanAct(itemId, { action: confirm ? 'confirm' : 'reject', clinician, role: 'clinician' });
        ctx.cortex.confirm(item.data.partition, item.data.id, { confirmer: clinician, confirm });
        return act;
      },
      sign: async (itemId, { clinician, role = 'treating_clinician', items, note = '' }) => {
        const item = ctx.board.get(itemId);
        if (!item || item.kind !== 'case_pack') throw new Error('not a case pack');
        const chosen = (items || item.data.draft).filter((i) => i.selected !== false);
        const act = ctx.board.humanAct(itemId, { action: 'sign', clinician, role, payload: { items: chosen.map((i) => i.code), note } });
        const decisionId = uid('dec');
        ctx.board.registerRelease(decisionId, act, Object.keys(CONTENT_CLASSES));
        ctx.cases.patch(item.patient, { decision: { id: decisionId, clinician, items: chosen } });
        const briefer = ctx.runtime.seat('case_briefer');
        await briefer.send('clinician_decision', 'dispatcher', item.patient, {
          case_id: item.case_id, decision_id: decisionId, signed_by: clinician, role, note,
          items: chosen.map(({ code, patient_text, patient_text_en, clinician_text }) => ({ code, patient_text, patient_text_en, clinician_text })),
          signature: act.sig,
        });
        return { decisionId, act };
      },
    },
    pacs: { newStudy: (patient) => ctx.runtime.seat('imaging_reader').onNewStudy(patient) },
  };

  ctx.reset = () => {
    for (const id of ['companion', 'dispatcher', 'renal_guardian']) ctx.runtime.seat(id).reset?.();
    ctx.bus.reset(); ctx.board.reset(); ctx.cases.reset(); ctx.cortex.wipeAll();
    ctx.runtime.manifests.clear(); ctx.runtime.history.clear(); ctx.runtime.loadSeedManifests();
    for (const [id, s] of ctx.runtime.seats) { if (s.shadow) ctx.runtime.seats.delete(id); else s.metrics = { messages_in: 0, messages_out: 0, tool_calls: 0, refusals: 0, gates_opened: 0, tc: 0, errors: 0 }; }
    hub.reset();
  };
  return ctx;
}
