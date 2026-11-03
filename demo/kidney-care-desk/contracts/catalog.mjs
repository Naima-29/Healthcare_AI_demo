// CONTRACT 0 - The shared vocabulary of the Kidney Care Desk.
// Refusals compile into hard runtime blocks: each refusal token blocks a set of
// actions (checked at the tool layer) and/or content classes (checked on every
// outbound message by the guard and by the Safety Verifier).

export const REFUSALS = {
  diagnose:                       { actions: ['patient.state_diagnosis'], content: ['diagnosis_claim'], label: 'Never diagnose' },
  prescribe:                      { actions: ['medication.prescribe'], content: ['prescription'], label: 'Never prescribe' },
  change_dose:                    { actions: ['medication.change_dose'], content: ['dose_instruction'], label: 'Never change or stop a dose' },
  disclose_imaging_before_review: { actions: ['patient.disclose_imaging'], content: ['imaging_result'], label: 'Never disclose an imaging result before clinician review' },
  disclose_genetics_to_patient:   { actions: ['patient.disclose_genetics'], content: ['genetic_result'], label: 'Never disclose genetic findings to the patient' },
  contact_clinician_directly:     { actions: ['clinician.direct_message'], content: [], label: 'Never contact a clinician directly (only via Dispatcher)' },
  answer_patient_directly:        { actions: ['patient.send'], content: [], label: 'Never answer the patient directly' },
  rank_clinicians:                { actions: ['routing.rank_people'], content: [], label: 'Never rank clinicians by measured behaviour' },
  label_as_diagnosis:             { actions: ['imaging.label_diagnosis'], content: ['diagnosis_claim'], label: 'Never call a model output a diagnosis' },
  report_to_patient:              { actions: ['patient.send', 'patient.disclose_imaging'], content: [], label: 'Never report to the patient' },
  alter_image:                    { actions: ['pacs.write'], content: [], label: 'Never alter an image' },
  reclassify_variants:            { actions: ['genomics.reclassify'], content: [], label: 'Never reclassify a variant' },
  pass_failed_check:              { actions: ['verifier.override'], content: [], label: 'Never pass a failed check' },
  alter_record:                   { actions: ['fhir.write', 'pacs.write'], content: [], label: 'Never alter a clinical record' },
  decide:                         { actions: ['case.decide'], content: [], label: 'Never take the clinical decision' },
  send_to_patient:                { actions: ['patient.send'], content: [], label: 'Never send anything to the patient' },
  store_without_evidence:         { actions: ['cortex.store_unevidenced'], content: [], label: 'Never store a rule without evidence' },
  auto_confirm_rule:              { actions: ['cortex.confirm_rule'], content: [], label: 'Never auto-confirm a rule' },
  write_general_weights:          { actions: ['model.train_general'], content: [], label: 'Never write patient data into general model weights' },
  transmit_identifiable_external: { actions: ['llm.external_identifiable'], content: [], label: 'Never send identifiable data outside the perimeter' },
};

export const GATES = {
  clinical_advice:     { signer: 'treating_clinician', label: 'Any clinical advice to the patient' },
  urgent_ack:          { signer: 'oncall_clinician', label: 'Urgent escalation acknowledged' },
  radiologist_review:  { signer: 'radiologist', label: 'Imaging signal reviewed' },
  genetic_disclosure:  { signer: 'geneticist', label: 'Genetic findings disclosed' },
  nephrologist_review: { signer: 'nephrologist', label: 'Medication change reviewed' },
  clinician_signoff:   { signer: 'treating_clinician', label: 'Case pack signed' },
  rule_confirmation:   { signer: 'clinician', label: 'New rule confirmed' },
  none:                { signer: null, label: 'No human gate (automatic)' },
};

// MCP servers (the "surfaces" a seat may touch) and their tools.
export const SURFACES = {
  fhir: ['fhir.get_patient', 'fhir.get_observations', 'fhir.get_conditions', 'fhir.get_medications'],
  pacs: ['pacs.list_studies', 'pacs.fetch_study'],
  imaging: ['ct.classify', 'ct.gradcam_heatmap', 'ct.quality_ood_check'],
  lab: ['lab.parse_report', 'lab.latest'],
  genomics: ['genomics.get_panel'],
  wearables: ['wearables.read'],
  oncall: ['oncall.who_is_on_call', 'oncall.page'],
  channel: ['channel.send', 'channel.receive'],
  km: ['km.query', 'km.check_conflicts', 'km.red_flags', 'km.interpret_variant', 'km.ckd_stage'],
  cortex: ['cortex.evidence_append', 'cortex.knowledge_propose', 'cortex.knowledge_read'],
};

export const A2A_TYPES = [
  'symptom_report', 'red_flag_escalation', 'medication_report', 'data_request', 'data_delivered',
  'imaging_signal', 'therapy_conflict', 'case_pack_ready', 'clinician_decision', 'acknowledgment',
];

export const SEAT_IDS = [
  'companion', 'dispatcher', 'imaging_reader', 'genomics_interpreter',
  'renal_guardian', 'safety_verifier', 'case_briefer', 'archivist',
];
