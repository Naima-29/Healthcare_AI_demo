// CLI: runs the whole golden path headless and prints what each human sees.
//   DEMO_PACE=0 node scripts/run-golden-path.mjs
process.env.DEMO_PACE ??= '0';
process.env.CORTEX_DIR ??= 'data/runtime/cortex-cli';
const { createDesk } = await import('../backend/desk.mjs');
const { autoplay } = await import('../backend/scenario/goldenPath.mjs');
const { hub } = await import('../backend/core/hub.mjs');

const ctx = createDesk();
ctx.cortex.wipeAll();
const show = new Set(['scenario', 'director_out', 'patient_in', 'patient_out', 'refusal', 'gate', 'page', 'error', 'a2a_rejected', 'verification']);
hub.on('event', (e) => {
  if (e.kind === 'a2a' && e.data.type !== 'acknowledgment') console.log(`  A2A  ${e.data.from.padEnd(20)} → ${e.data.to.padEnd(20)} ${e.data.type}${e.data.verified ? ' ✓sig' : ' ✗sig'}`);
  if (!show.has(e.kind)) return;
  const d = e.data;
  const line = {
    scenario: () => d.op === 'step' ? `\n=== STEP ${d.n}: ${d.title}` : null,
    director_out: () => `  CHIEF OF STAFF » ${d.text.replace(/\n/g, '\n                   ')}`,
    patient_in: () => `  PATIENT » ${d.text}`,
    patient_out: () => `  COMPANION » ${d.text.replace(/\n/g, '\n              ')}`,
    refusal: () => `  ⛔ REFUSAL  ${d.seat} v${d.version}: ${d.label} (${d.refusal}) ${d.action || d.content_class || ''}`,
    gate: () => `  ✍ GATE  ${d.kind}/${d.gate}: ${d.action} by ${d.clinician} (sig ${d.verified ? 'verified' : 'INVALID'})`,
    page: () => `  📟 PAGE  ${d.name} (${d.pager}): ${d.message}`,
    error: () => `  !! ERROR ${d.seat}: ${d.message}`,
    a2a_rejected: () => `  !! A2A REJECTED ${JSON.stringify(d)}`,
    verification: () => `  ✔ VERIFIER ${d.type}: ${d.passed ? 'passed' : 'FAILED'} (${d.checks.length} checks)`,
  }[e.kind]?.();
  if (line) console.log(line);
});
await autoplay(ctx);
await new Promise((r) => setTimeout(r, 500));
console.log('\n=== MEMORY (Cortex)');
for (const p of ctx.cortex.partitions()) {
  console.log(`  [${p}] evidence chain: ${JSON.stringify(ctx.cortex.verifyChain(p))}`);
  for (const k of ctx.cortex.knowledge(p)) console.log(`     ${k.status.padEnd(10)} ${k.key}: ${k.value} (by ${k.confirmer ?? '—'})`);
}
console.log('\n=== METERING');
for (const id of ctx.runtime.ids()) { const s = ctx.runtime.seat(id); console.log(`  ${id.padEnd(22)} TC ${s.metrics.tc.toFixed(1).padStart(5)}  refusals ${s.metrics.refusals}  errors ${s.metrics.errors}`); }
process.exit(0);
