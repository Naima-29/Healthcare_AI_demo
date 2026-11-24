"""HTTP API for the control plane (Director approvals). Authentication lives here; every authorisation rule
lives in ControlPlane, so a new transport cannot accidentally skip one.

Auth fails closed: with no OIDC issuer configured every call is a 401. A static dev token exists for local work
only and is refused by Settings.validate_for_env outside dev/test.
"""

from __future__ import annotations

import asyncio
import hmac
from collections.abc import Callable
from typing import Any, Protocol

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from .control_plane import Actor, Conflict, ControlPlane, ControlPlaneError, Forbidden, NotFound
from .settings import Settings


class Authenticator(Protocol):
    async def __call__(self, request: Request) -> Actor: ...


def _bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(401, "missing bearer token", headers={"WWW-Authenticate": "Bearer"})
    return token


class DenyAll:
    async def __call__(self, request: Request) -> Actor:
        raise HTTPException(401, "authentication is not configured", headers={"WWW-Authenticate": "Bearer"})


class DevTokenAuthenticator:
    """Local development only: one static token acts as a Director."""

    def __init__(self, token: str) -> None:
        self._token = token

    async def __call__(self, request: Request) -> Actor:
        if not hmac.compare_digest(_bearer(request), self._token):
            raise HTTPException(401, "bad token", headers={"WWW-Authenticate": "Bearer"})
        return Actor("dev_director", frozenset({"director"}))


class JwtAuthenticator:
    """Validates an OIDC access token (Keycloak): signature, issuer, audience, expiry. Roles come from
    `realm_access.roles`. `key_resolver(token)` returns the verification key (a JWKS lookup in production)."""

    def __init__(self, *, issuer: str, audience: str, key_resolver: Callable[[str], Any],
                 algorithms: tuple[str, ...] = ("RS256", "ES256")) -> None:
        self._issuer, self._audience, self._keys, self._algs = issuer, audience, key_resolver, list(algorithms)

    async def __call__(self, request: Request) -> Actor:
        token = _bearer(request)
        try:
            key = await asyncio.to_thread(self._keys, token)
            claims = jwt.decode(token, key, algorithms=self._algs, issuer=self._issuer, audience=self._audience,
                                options={"require": ["exp", "iss", "aud", "sub"]})
        except (jwt.PyJWTError, OSError):
            raise HTTPException(401, "invalid token", headers={"WWW-Authenticate": "Bearer"}) from None
        roles = frozenset(claims.get("realm_access", {}).get("roles", []))
        return Actor(str(claims.get("preferred_username") or claims["sub"]), roles)


def build_authenticator(settings: Settings) -> Authenticator:
    if settings.oidc_issuer and settings.oidc_audience and settings.oidc_jwks_url:
        client = jwt.PyJWKClient(settings.oidc_jwks_url, cache_keys=True, lifespan=600)
        return JwtAuthenticator(issuer=settings.oidc_issuer, audience=settings.oidc_audience,
                                key_resolver=lambda tok: client.get_signing_key_from_jwt(tok).key)
    if settings.env in ("dev", "test") and settings.dev_token:
        return DevTokenAuthenticator(settings.dev_token)
    return DenyAll()


class ProposeBody(BaseModel):
    instruction: str = Field(min_length=8, max_length=4000)
    seat: str | None = Field(default=None, description="Amend this live seat; omit to create a new one")
    name: str | None = Field(default=None, max_length=60)


class RejectBody(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


def build_router(plane: ControlPlane, authenticate: Authenticator) -> APIRouter:
    router = APIRouter(prefix="/v1/manifests", tags=["control-plane"])

    async def actor(request: Request) -> Actor:
        return await authenticate(request)

    def run(fn: Callable[..., Any], *args: Any) -> Any:
        return asyncio.to_thread(fn, *args)

    @router.post("/drafts", status_code=201)
    async def propose(body: ProposeBody, who: Actor = Depends(actor)) -> dict[str, Any]:
        draft, compiled = await plane.propose(who, body.instruction, body.seat, body.name)
        return {**draft.to_json(), "notes": compiled.notes}

    @router.get("/drafts")
    async def list_drafts(status: str | None = "pending", who: Actor = Depends(actor)) -> list[dict[str, Any]]:
        who.require("director")
        if status not in (None, "pending", "approved", "rejected", "superseded"):
            raise HTTPException(422, "bad status")
        return [d.to_json() for d in await run(plane.store.list_drafts, status)]

    @router.get("/drafts/{draft_id}")
    async def get_draft(draft_id: str, who: Actor = Depends(actor)) -> dict[str, Any]:
        who.require("director")
        draft = await run(plane.store.get_draft, draft_id)
        if draft is None:
            raise NotFound("no such draft")
        return draft.to_json()  # type: ignore[no-any-return]

    @router.post("/drafts/{draft_id}/approve")
    async def approve(draft_id: str, who: Actor = Depends(actor)) -> dict[str, Any]:
        return (await run(plane.approve, who, draft_id)).to_json()  # type: ignore[no-any-return]

    @router.post("/drafts/{draft_id}/reject")
    async def reject(draft_id: str, body: RejectBody, who: Actor = Depends(actor)) -> dict[str, Any]:
        return (await run(plane.reject, who, draft_id, body.reason)).to_json()  # type: ignore[no-any-return]

    @router.post("/{seat}/rollback")
    async def rollback(seat: str, who: Actor = Depends(actor)) -> dict[str, Any]:
        return (await run(plane.rollback, who, seat)).model_dump(mode="json")  # type: ignore[no-any-return]

    return router


def install_error_handlers(app: Any) -> None:
    from fastapi.responses import JSONResponse

    for exc_type, code in ((Forbidden, 403), (NotFound, 404), (Conflict, 409), (ControlPlaneError, 422)):
        app.add_exception_handler(
            exc_type, lambda request, exc, code=code: JSONResponse({"detail": str(exc)}, status_code=code)
        )
