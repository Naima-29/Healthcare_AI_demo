"""Rule-pass patterns ported verbatim from the demo compiler (demo/kidney-care-desk/backend/compiler.mjs).

Generated once from the JS source; edit here from now on. EN / IT / FR. Matched case-insensitively.
"""

from __future__ import annotations

import re

NEG = r"(?:non|mai|never|do not|don't|must not|jamais|ne\s+\w+\s+pas)"

REFUSAL_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(NEG + r"[^.]{0,60}(comunic|dire|rivel|riferi|disclos|tell|report|share|condivid|communiqu)[^.]{0,80}(imaging|\btac\b|\bct\b|immagin|radiolog|esame strumentale|scan|imagerie)", re.I), "disclose_imaging_before_review"),
    (re.compile(NEG + r"[^.]{0,30}(diagnos)", re.I), "diagnose"),
    (re.compile(NEG + r"[^.]{0,30}(prescriv|prescrib|prescri)", re.I), "prescribe"),
    (re.compile(NEG + r"[^.]{0,40}(modific|cambi|change|stop|sospend|interromp|aument|ridu|alter)[^.]{0,20}(dos|terapi|farmac|medic|dose)", re.I), "change_dose"),
    (re.compile(r"(?:non|mai|never|do not|don't|must not|jamais|ne\s+\w+\s+pas)[^.]{0,60}(genetic|genetich|dna|varian)[^.]{0,40}(pazient|patient)|(?:non|mai|never|do not|don't|must not|jamais|ne\s+\w+\s+pas)[^.]{0,30}(disclos|comunic|rivel)[^.]{0,40}(genetic|genetich|dna)", re.I), "disclose_genetics_to_patient"),
    (re.compile(NEG + r"[^.]{0,40}(contatt|contact|scriv)[^.]{0,30}(medic|clinic|doctor|staff)[^.]{0,20}(diretta|directly)", re.I), "contact_clinician_directly"),
    (re.compile(NEG + r"[^.]{0,30}(rispond|answer|répond)[^.]{0,30}(pazient|patient)", re.I), "answer_patient_directly"),
    (re.compile(NEG + r"[^.]{0,30}(classific|rank|ordin|valut)[^.]{0,30}(medic|clinic|person|people)", re.I), "rank_clinicians"),
    (re.compile(NEG + r"[^.]{0,40}(call it|chiamar|definir|presentar)[^.]{0,20}(a )?(diagnos)", re.I), "label_as_diagnosis"),
    (re.compile(NEG + r"[^.]{0,30}(report|riferi|invi|send|mand)[^.]{0,30}(anything )?(al |to the )?(pazient|patient)", re.I), "send_to_patient"),
    (re.compile(NEG + r"[^.]{0,30}(alter|modific)[^.]{0,20}(image|immagin)", re.I), "alter_image"),
    (re.compile(NEG + r"[^.]{0,30}(reclassif|riclassific)", re.I), "reclassify_variants"),
    (re.compile(NEG + r"[^.]{0,30}(pass|lasci\w* passare|approv)[^.]{0,30}(fail|fallit|non superat)", re.I), "pass_failed_check"),
    (re.compile(NEG + r"[^.]{0,30}(alter|modific|scriv|write)[^.]{0,30}(record|cartella|fascicolo)", re.I), "alter_record"),
    (re.compile(NEG + r"[^.]{0,20}(decid|take the decision|prend\w* la decisione)", re.I), "decide"),
    (re.compile(NEG + r"[^.]{0,30}(store|salv|memorizz)[^.]{0,30}(without evidence|senza evidenz|senza prov)", re.I), "store_without_evidence"),
    (re.compile(NEG + r"[^.]{0,30}(auto-?confirm|conferm\w* da sol|auto-?conferm)", re.I), "auto_confirm_rule"),
    (re.compile(NEG + r"[^.]{0,60}(general model weights|pesi generali|pesi del modello)", re.I), "write_general_weights"),
    (re.compile(NEG + r"[^.]{0,60}(identifiable|identificabil)[^.]{0,40}(outside|fuori|esterno|external)", re.I), "transmit_identifiable_external"),
    # Core addition: the Chief of Staff never puts a manifest live by itself.
    (re.compile(NEG + r"[^.]{0,40}(activ|attiv|go live|put live|deploy|metter\w* in produzione)", re.I), "activate_without_approval"),
]

TOOL_PATTERNS: list[tuple[re.Pattern[str], list[str], str | None]] = [
    (re.compile(r"wearable|indossabil|smartwatch|hrv|pressione domiciliare", re.I), ["wearables.read"], "wearables"),
    (re.compile(r"telegram|whatsapp|messagg|chat|telefon|phone|patient channel|canale", re.I), ["channel.send", "channel.receive"], "channel"),
    (re.compile(r"\bpacs\b|dicom|\btac\b|\bct\b|imaging|immagin", re.I), ["pacs.list_studies", "pacs.fetch_study"], "pacs"),
    (re.compile(r"classific|heat ?map|mappa di calore|grad-?cam", re.I), ["ct.classify", "ct.gradcam_heatmap", "ct.quality_ood_check"], "imaging"),
    (re.compile(r"fhir|cartella|record|ehr|fascicolo|egfr|creatinin|farmac|medication", re.I), ["fhir.get_patient", "fhir.get_observations", "fhir.get_conditions", "fhir.get_medications"], "fhir"),
    (re.compile(r"laborator|lab report|referto|esami del sangue|blood test", re.I), ["lab.parse_report"], "lab"),
    (re.compile(r"genetic|genom|\bdna\b|panel|pannello", re.I), ["genomics.get_panel"], "genomics"),
    (re.compile(r"on-?call|reperibil|guardia|cercapersone|page", re.I), ["oncall.who_is_on_call", "oncall.page"], "oncall"),
    (re.compile(r"linee guida|guideline|knowledge|conoscenz|citazion|citation|red.?flag|conflitt|conflict", re.I), ["km.query", "km.red_flags", "km.check_conflicts"], "km"),
    (re.compile(r"memori|cortex|regol|rule|evidenz|evidence", re.I), ["cortex.evidence_append", "cortex.knowledge_propose", "cortex.knowledge_read"], "cortex"),
    (re.compile(r"board|bacheca|caso|case pack|sbar", re.I), ["board.create_item", "board.update_item"], None),
]

GATE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"radiolog", re.I), "radiologist_review"),
    (re.compile(r"genetist|geneticist", re.I), "genetic_disclosure"),
    (re.compile(r"nefrolog|nephrolog", re.I), "nephrologist_review"),
    (re.compile(r"reperibil|on-?call", re.I), "urgent_ack"),
    (re.compile(r"(medico|clinician|doctor|curante)[^.]{0,40}(approv|firm|sign|rivist|review|conferm)|(approv|firm|sign|review|conferm)[^.]{0,40}(medico|clinician|doctor)", re.I), "clinical_advice"),
    # Core addition
    (re.compile(r"(director|direttore|directeur)[^.]{0,40}(approv|firm|sign|valid)", re.I), "director_approval"),
]
