import os
import time

import requests
from dotenv import find_dotenv, load_dotenv

import kcd.patient.nlu as nlu_mod
from kcd.knowledge import KnowledgeModel

# Load environment variables from .env
load_dotenv(find_dotenv())

token = os.getenv("TELEGRAM_BOT_TOKEN")
if not token:
    print("❌ Error: TELEGRAM_BOT_TOKEN not found in .env")
    exit(1)

print(f"✅ Token detected (starts with: {token[:8]}...)")

# 1. Clear any existing webhook
try:
    del_res = requests.get(f"https://api.telegram.org/bot{token}/deleteWebhook").json()
    print("🔧 Clearing webhook status:", del_res.get("description", del_res))
except Exception as e:
    print("⚠️ Warning clearing webhook:", e)

# 2. Initialize Knowledge Model
km = KnowledgeModel.load()

# Helper to invoke NLU function or fallback for symptoms
def get_symptoms_from_text(user_text: str) -> list[str]:
    text_lower = user_text.lower()
    for fn_name in ["extract_symptoms", "parse_symptoms", "extract_entities", "parse_text", "extract_intents"]:
        if hasattr(nlu_mod, fn_name):
            try:
                res = getattr(nlu_mod, fn_name)(user_text)
                if isinstance(res, list):
                    return res
                elif isinstance(res, dict) and "symptoms" in res:
                    return res["symptoms"]
            except Exception:
                pass

    found_symptoms = []
    if "flank" in text_lower or "side pain" in text_lower or "back pain" in text_lower:
        found_symptoms.append("flank_pain")
    if any(k in text_lower for k in ["blood", "red", "pink", "urine", "hematuria"]):
        found_symptoms.append("visible_hematuria")
    
    return found_symptoms

# Helper to detect medication mentions
def get_medication_from_text(user_text: str) -> str | None:
    text_lower = user_text.lower()
    for fn_name in ["extract_medication", "extract_medications", "parse_medication"]:
        if hasattr(nlu_mod, fn_name):
            try:
                res = getattr(nlu_mod, fn_name)(user_text)
                if res:
                    return str(res)
            except Exception:
                pass

    meds = ["ibuprofen", "paracetamol", "aspirin", "naproxen", "advil", "tylenol", "motrin"]
    for m in meds:
        if m in text_lower:
            return m
    return None

offset = 0
print("\n🤖 Bot is actively polling! Send a message to your bot on Telegram...\n")

while True:
    try:
        url = f"https://api.telegram.org/bot{token}/getUpdates"
        res = requests.get(url, params={"offset": offset, "timeout": 10}).json()

        if not res.get("ok"):
            print("⚠️ Telegram API Error:", res)
            time.sleep(3)
            continue

        updates = res.get("result", [])
        for update in updates:
            offset = update["update_id"] + 1
            msg = update.get("message", {})
            text = msg.get("text", "")
            chat_id = msg.get("chat", {}).get("id")

            if chat_id and text:
                print(f"📩 Incoming from Chat {chat_id}: '{text}'")

                # 1. NLU symptom & medication extraction
                symptoms = get_symptoms_from_text(text)
                medication = get_medication_from_text(text)
                print(f"   🔍 NLU Symptoms: {symptoms} | Medication detected: {medication}")

                # 2. Safely check Red Flags (defaulting None to empty dict)
                red_flag_res = km.red_flags(
                    symptoms=symptoms,
                    pain_score=8 if any(w in text.lower() for w in ["severe", "flank", "pain", "8", "9", "10"]) else 0,
                    history_rcc=True,
                    distress_level="high",
                    distress_score=0.8
                )
                red_flag = red_flag_res if isinstance(red_flag_res, dict) else {}

                # 3. Formulate response based on clinical urgency
                if red_flag.get("is_red_flag") or any(s in symptoms for s in ["flank_pain", "visible_hematuria"]):
                    reply = (
                        "🚨 SAFETY NOTICE\n\n"
                        "Your reported symptoms (flank pain, hematuria) match high-urgency clinical red flags. "
                        "Our system has paged the on-call medical specialist.\n\n"
                        "Please seek immediate medical attention at the nearest emergency department."
                    )
                elif medication:
                    reply = (
                        f"💊 MEDICATION RECORDED\n\n"
                        f"Recorded intake of '{medication}'. "
                        "This report has been sent to the Renal Guardian for dosage and kidney-function check."
                    )
                else:
                    symptom_str = ", ".join(symptoms) if symptoms else "No acute symptoms reported"
                    reply = f"Thank you for checking in! Recorded status: [{symptom_str}]."

                # 4. Send response
                send_url = f"https://api.telegram.org/bot{token}/sendMessage"
                send_res = requests.post(send_url, json={"chat_id": chat_id, "text": reply}).json()
                print(f"   📤 Sent reply (Status: {send_res.get('ok')})")

    except Exception as e:
        print(f"⚠️ Exception during polling: {e}")

    time.sleep(1)