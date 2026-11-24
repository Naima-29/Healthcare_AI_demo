"""Director approval API: authentication, authorisation, and the happy path over HTTP."""

import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient
from kcd_core.api import (
    DenyAll,
    DevTokenAuthenticator,
    JwtAuthenticator,
    build_authenticator,
    build_router,
    install_error_handlers,
)
from kcd_core.control_plane import ControlPlane, InMemoryManifestStore
from kcd_core.settings import Settings

ISS, AUD = "https://id.hospital.test/realms/kcd", "kcd-core"
AMEND = "Never send the CT report to the patient before the clinician has seen it."


@pytest.fixture(scope="module")
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def token(key, *, roles=("director",), iss=ISS, aud=AUD, exp=3600, sub="u1"):
    claims = {"sub": sub, "preferred_username": "dr_ferri", "iss": iss, "aud": aud,
              "exp": int(time.time()) + exp, "realm_access": {"roles": list(roles)}}
    return jwt.encode(claims, key, algorithm="RS256")


def client(desk, auth):
    plane = ControlPlane(registry=desk.registry, store=InMemoryManifestStore(), audit=desk.audit, clock=desk.clock)
    app = FastAPI()
    app.include_router(build_router(plane, auth))
    install_error_handlers(app)
    return TestClient(app)


@pytest.fixture
def api(desk, rsa_key):
    return client(desk, JwtAuthenticator(issuer=ISS, audience=AUD, key_resolver=lambda t: rsa_key.public_key()))


def hdr(t):
    return {"Authorization": f"Bearer {t}"}


class TestAuth:
    def test_no_token_is_401(self, api):
        assert api.get("/v1/manifests/drafts").status_code == 401

    def test_unconfigured_auth_denies_everything(self, desk):
        c = client(desk, DenyAll())
        assert c.post("/v1/manifests/drafts", json={"instruction": AMEND}).status_code == 401

    @pytest.mark.parametrize("kw", [dict(iss="https://evil"), dict(aud="other"), dict(exp=-10)])
    def test_wrong_issuer_audience_or_expired_is_401(self, api, rsa_key, kw):
        assert api.get("/v1/manifests/drafts", headers=hdr(token(rsa_key, **kw))).status_code == 401

    def test_token_signed_with_another_key_is_401(self, api):
        other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        assert api.get("/v1/manifests/drafts", headers=hdr(token(other))).status_code == 401

    def test_alg_none_is_refused(self, api):
        forged = jwt.encode({"sub": "x", "iss": ISS, "aud": AUD, "exp": int(time.time()) + 99,
                             "realm_access": {"roles": ["director"]}}, key=None, algorithm="none")
        assert api.get("/v1/manifests/drafts", headers=hdr(forged)).status_code == 401

    def test_authenticated_but_not_a_director_is_403(self, api, rsa_key):
        t = token(rsa_key, roles=("clinician",))
        assert api.post("/v1/manifests/drafts", json={"instruction": AMEND, "seat": "companion"},
                        headers=hdr(t)).status_code == 403
        assert api.get("/v1/manifests/drafts", headers=hdr(t)).status_code == 403

    def test_dev_token_only_when_configured_and_only_in_dev(self):
        assert isinstance(build_authenticator(Settings(env="dev", dev_token="t")), DevTokenAuthenticator)
        assert isinstance(build_authenticator(Settings(env="prod", dev_token="t")), DenyAll)
        assert isinstance(build_authenticator(Settings(env="dev")), DenyAll)


class TestFlow:
    def test_propose_review_approve_over_http(self, api, rsa_key, desk):
        h = hdr(token(rsa_key))
        r = api.post("/v1/manifests/drafts", json={"instruction": AMEND, "seat": "companion"}, headers=h)
        assert r.status_code == 201
        draft = r.json()
        assert draft["status"] == "pending" and draft["diff"]["changes"] and draft["base_version"] == 1

        assert [d["id"] for d in api.get("/v1/manifests/drafts", headers=h).json()] == [draft["id"]]
        assert api.get(f"/v1/manifests/drafts/{draft['id']}", headers=h).json()["id"] == draft["id"]

        ok = api.post(f"/v1/manifests/drafts/{draft['id']}/approve", headers=h)
        assert ok.status_code == 200 and ok.json()["status"] == "approved"
        assert ok.json()["decided_by"] == "dr_ferri"
        assert desk.registry.get("companion").version == 2

        again = api.post(f"/v1/manifests/drafts/{draft['id']}/approve", headers=h)
        assert again.status_code == 200  # idempotent

        rb = api.post("/v1/manifests/companion/rollback", headers=h)
        assert rb.status_code == 200 and rb.json()["version"] == 3

    def test_error_mapping(self, api, rsa_key):
        h = hdr(token(rsa_key))
        assert api.get("/v1/manifests/drafts/nope", headers=h).status_code == 404
        ghost = {"instruction": AMEND, "seat": "ghost"}
        assert api.post("/v1/manifests/drafts", json=ghost, headers=h).status_code == 404
        assert api.post("/v1/manifests/drafts", json={"instruction": "x"}, headers=h).status_code == 422
        d = api.post("/v1/manifests/drafts", json={"instruction": AMEND, "seat": "companion"}, headers=h).json()
        rejected = api.post(f"/v1/manifests/drafts/{d['id']}/reject", json={"reason": "too broad"}, headers=h)
        assert rejected.status_code == 200
        assert api.post(f"/v1/manifests/drafts/{d['id']}/approve", headers=h).status_code == 409
