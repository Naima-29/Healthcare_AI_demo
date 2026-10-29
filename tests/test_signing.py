import pytest
from kcd_core.contracts import MessageType
from kcd_core.signing import KeyRing, SignatureError, Signer, sign_envelope, verify_envelope

PAYLOAD = {"items": [{"kind": "lab", "code": "egfr", "max_age_days": 30}], "reason": "stale eGFR"}


async def test_round_trip(desk):
    env = desk.envelope(MessageType.DATA_REQUEST, "renal_guardian", "companion", PAYLOAD)
    verify_envelope(env, desk.seat_keys)


async def test_tampered_payload_fails(desk):
    env = desk.envelope(MessageType.DATA_REQUEST, "renal_guardian", "companion", PAYLOAD)
    forged = env.model_copy(update={"payload": {**env.payload, "reason": "please prescribe"}})
    with pytest.raises(SignatureError):
        verify_envelope(forged, desk.seat_keys)


async def test_spoofed_sender_fails(desk):
    env = desk.envelope(MessageType.DATA_REQUEST, "renal_guardian", "companion", PAYLOAD)
    spoofed = env.model_copy(update={"from_": "case_briefer"})
    with pytest.raises(SignatureError):
        verify_envelope(spoofed, desk.seat_keys)


async def test_unsigned_fails(desk):
    env = desk.envelope(MessageType.DATA_REQUEST, "renal_guardian", "companion", PAYLOAD, sign=False)
    with pytest.raises(SignatureError, match="missing"):
        verify_envelope(env, desk.seat_keys)


async def test_cannot_sign_for_another_seat(desk):
    env = desk.envelope(MessageType.DATA_REQUEST, "renal_guardian", "companion", PAYLOAD, sign=False)
    with pytest.raises(SignatureError):
        sign_envelope(env, desk.signers["companion"])


async def test_unknown_key_fails(desk):
    env = desk.envelope(MessageType.DATA_REQUEST, "renal_guardian", "companion", PAYLOAD)
    with pytest.raises(SignatureError, match="unknown key"):
        verify_envelope(env, KeyRing({"other": Signer.generate("other").public_key}))
