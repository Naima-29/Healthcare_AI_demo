"""The HTTP API the frontend speaks (same shape as the stage demo's backend/server.mjs), on the real desk."""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from kcd.scenarios.edge_cases import FIXTURES, PATIENT, EdgeCases, real_model_checks
from kcd.web import director, golden
from kcd.web.hub import Hub
from kcd.web.session import DeskError, Session, real_model_available

STATIC = Path(__file__).resolve().parent / "static"
MAX_TEXT = 4000


async def _body(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except ValueError as e:
        raise DeskError("the request body must be JSON", 400) from e
    if not isinstance(body, dict):
        raise DeskError("the request body must be a JSON object", 400)
    return body


def _text(body: dict[str, Any], key: str = "text") -> str:
    value = body.get(key)
    if not isinstance(value, str) or not value.strip():
        raise DeskError(f"'{key}' is required", 422)
    if len(value) > MAX_TEXT:
        raise DeskError(f"'{key}' is longer than {MAX_TEXT} characters", 422)
    return value


def _run_edge_cases(with_real_model: bool) -> list[dict[str, Any]]:
    """In its own thread, event loop and working copy: the checks never touch the live desk's patients."""
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        shutil.copytree(FIXTURES / PATIENT, work / PATIENT)
        checks = asyncio.run(EdgeCases(work).run())
        if with_real_model:
            checks += asyncio.run(real_model_checks(work))
    return [vars(c) for c in checks]


def create_app() -> FastAPI:
    hub = Hub()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.session = await Session.create(hub)
        yield
        await app.state.session.close()

    app = FastAPI(title="Kidney Care Desk", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    # Local demo with no login: only answer when reached as localhost, so a web page elsewhere cannot rebind to it.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])

    def desk() -> Session:
        return app.state.session

    @app.exception_handler(DeskError)
    async def _desk_error(_: Request, e: DeskError) -> JSONResponse:
        return JSONResponse({"error": str(e)}, status_code=e.status)

    @app.get("/api/state")
    async def state() -> dict[str, Any]:
        return {**desk().state(), "steps": golden.STEPS}

    @app.get("/events")
    async def events(request: Request) -> StreamingResponse:
        queue = hub.subscribe()

        async def stream() -> AsyncIterator[str]:
            try:
                yield ": connected\n\n"
                while not await request.is_disconnected():
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=10)
                    except TimeoutError:
                        yield ": still here\n\n"
                        continue
                    yield f"data: {json.dumps(event, default=str)}\n\n"
            finally:
                hub.unsubscribe(queue)

        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    # ------------------------------------------------------------------ the three doors
    @app.post("/api/director")
    async def post_director(request: Request) -> dict[str, Any]:
        return director.handle(desk(), _text(await _body(request)))

    @app.post("/api/patient/message")
    async def patient_message(request: Request) -> dict[str, bool]:
        body = await _body(request)
        await desk().patient_message(_text(body), body.get("features"))
        return {"ok": True}

    @app.post("/api/patient/attach-lab")
    async def attach_lab(request: Request) -> dict[str, bool]:
        await desk().attach_lab((await _body(request)).get("photo"))
        return {"ok": True}

    @app.post("/api/patient/select")
    async def select_patient(request: Request) -> dict[str, bool]:
        desk().select_patient(_text(await _body(request), "patient"))
        return {"ok": True}

    @app.post("/api/patient/erase")
    async def erase_patient(request: Request) -> dict[str, Any]:
        return desk().erase_patient((await _body(request)).get("patient"))

    @app.post("/api/board/act")
    async def board_act(request: Request) -> dict[str, bool]:
        b = await _body(request)
        await desk().board_act(_text(b, "item"), _text(b, "action"), _text(b, "clinician"), b.get("items"),
                               str(b.get("note") or ""))
        return {"ok": True}

    # ------------------------------------------------------------------ CT studies
    async def _start_reading(study_uid: str | None, wait: bool) -> JSONResponse:
        session = desk()
        session.check_study(study_uid)
        patient = session.active
        if wait:
            result = await session.read_study(study_uid, patient)
            return JSONResponse({"status": result.status, "sent": result.sent, "duplicate": result.duplicate})
        session.spawn(session.read_study(study_uid, patient))
        return JSONResponse({"started": True}, status_code=202)

    @app.post("/api/pacs/new-study")
    async def new_study(request: Request) -> JSONResponse:
        b = await _body(request)
        return await _start_reading(b.get("study_uid"), bool(b.get("wait")))

    @app.post("/api/pacs/upload")
    async def upload_study(request: Request) -> JSONResponse:
        b = await _body(request)
        study_uid = desk().add_upload(str(b.get("name") or ""), _text_b64(b))
        return await _start_reading(study_uid, bool(b.get("wait")))

    def _text_b64(body: dict[str, Any]) -> str:
        data = body.get("data_base64")
        if not isinstance(data, str) or not data:
            raise DeskError("'data_base64' is required", 422)
        return data

    @app.post("/api/pacs/model")
    async def ct_model(request: Request) -> dict[str, bool]:
        desk().set_ct_model(_text(await _body(request), "mode"))
        return {"ok": True}

    @app.get("/api/evidence/{patient}/{evidence_id}")
    async def evidence(patient: str, evidence_id: str) -> Response:
        try:
            data, media = desk().evidence_image(patient, evidence_id)
        except LookupError as e:
            raise DeskError("no such evidence", 404) from e
        return Response(data, media_type=media, headers={"X-Content-Type-Options": "nosniff"})

    # ------------------------------------------------------------------ seats
    def _seat(seat: str) -> str:
        if seat not in desk().desk.registry.seats():
            raise DeskError(f"unknown seat {seat}", 404)
        return seat

    @app.get("/api/seats/{seat}")
    async def get_seat(seat: str) -> dict[str, Any]:
        return desk().seat_view(_seat(seat))

    @app.post("/api/seats/{seat}/compile")
    async def compile_seat(seat: str, request: Request) -> dict[str, Any]:
        return director.amend_seat(desk(), _seat(seat), _text(await _body(request), "instruction"))

    # ------------------------------------------------------------------ scenarios
    async def _exclusive(session: Session, work: Any) -> None:
        session.busy = True
        try:
            await work
        finally:
            session.busy = False

    def _start_scenario(work: Any) -> None:
        session = desk()
        if session.busy:
            work.close()
            raise DeskError("A scenario is already running.")
        session.busy = True
        session.spawn(_exclusive(session, work))

    @app.post("/api/scenario/step")
    async def scenario_step(request: Request) -> JSONResponse:
        b = await _body(request)
        n = b.get("n")
        if not any(s["n"] == n for s in golden.STEPS):
            raise DeskError("'n' must be a golden path step", 422)
        if b.get("wait"):
            await golden.run_step(desk(), n, pace=0)
            return JSONResponse({"ok": True})
        _start_scenario(golden.run_step(desk(), n))
        return JSONResponse({"started": True}, status_code=202)

    @app.post("/api/scenario/autoplay")
    async def scenario_autoplay() -> JSONResponse:
        _start_scenario(golden.autoplay(desk()))
        return JSONResponse({"started": True}, status_code=202)

    @app.post("/api/reset")
    async def reset() -> dict[str, bool]:
        await app.state.session.close()
        hub.reset()
        app.state.session = await Session.create(hub)
        return {"ok": True}

    @app.post("/api/edge-cases/run")
    async def edge_cases(request: Request) -> JSONResponse:
        session = desk()
        with_real = bool((await _body(request)).get("with_real_model"))
        if with_real and not real_model_available():
            raise DeskError("Chrislin's model needs its weights under models/kidney_project and the ct-model extras")
        if session.edge_running:
            raise DeskError("The edge cases are already running.")

        async def run() -> None:
            session.edge_running = True
            hub.publish("edge_cases", {"op": "start", "with_real_model": with_real})
            try:
                checks = await asyncio.to_thread(_run_edge_cases, with_real)
                session.edge_report = {"checks": checks, "with_real_model": with_real,
                                       "passed": sum(c["ok"] for c in checks), "total": len(checks)}
                hub.publish("edge_cases", {"op": "done", "passed": session.edge_report["passed"], "total": len(checks)})
            except Exception:
                hub.publish("edge_cases", {"op": "failed"})
                raise
            finally:
                session.edge_running = False

        session.spawn(run())
        return JSONResponse({"started": True}, status_code=202)

    app.mount("/", StaticFiles(directory=STATIC, html=True), name="frontend")
    return app
