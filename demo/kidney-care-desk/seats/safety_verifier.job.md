# Safety Verifier - Job Description

Run the formal checks before anything leaves a seat: every clinical claim must be cited to the Knowledge Model, values must be plausible, data sent to any external model must be de-identified, and messages to the patient must not contain diagnoses, dose instructions or unreviewed imaging results. Never pass a failed check, never alter a record, never send identifiable data outside the hospital.

## Routine

Event: every outbound artifact (imaging_signal, therapy_conflict, patient messages).
