// Loads the synthetic patient into a live HAPI FHIR server (transaction bundle).
//   FHIR_BASE_URL=http://localhost:8080/fhir node scripts/seed-live.mjs
// For Orthanc, upload your own de-identified kidney CT DICOM (e.g. a KiTS19 case)
// with PatientID=marco-r:  curl -X POST http://localhost:8042/instances --data-binary @case.dcm
import fs from 'node:fs';
const base = process.env.FHIR_BASE_URL;
if (!base) { console.error('Set FHIR_BASE_URL'); process.exit(1); }
const bundle = JSON.parse(fs.readFileSync('data/marco-r/fhir-bundle.json', 'utf8'));
const tx = {
  resourceType: 'Bundle', type: 'transaction',
  entry: bundle.entry.map(({ resource }) => ({ resource, request: { method: 'PUT', url: `${resource.resourceType}/${resource.id}` } })),
};
const r = await fetch(base, { method: 'POST', headers: { 'content-type': 'application/fhir+json' }, body: JSON.stringify(tx) });
console.log('HAPI FHIR:', r.status, r.ok ? 'patient marco-r loaded' : await r.text());
