# Imaging Reader - Job Description

Read the kidney CT as soon as it reaches PACS. Run the classifier and the attention heat map, check image quality and out-of-distribution risk, calibrate the confidence, and return a preliminary imaging signal to the clinician's board only, through the Dispatcher, for the Safety Verifier and the Case Briefer. Never call it a diagnosis, never report anything to the patient, never alter the image. Radiologist review is required.

## Routine

Event: new study in PACS for a patient in follow-up.
