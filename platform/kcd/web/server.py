"""Tiny HTTP server (stdlib only): static front end, JSON API for the three doors, server-sent events.

    python -m kcd.web                 # http://localhost:5173
    python -m kcd.web --light         # without Cortex-backed seats (no sqlalchemy needed)
    python -m kcd.web --port 8080
"""

from __future__ import annotations

import argparse
import asyncio
import json
import mimetypes
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from kcd.web.app import ActionError, DeskApp
from kcd.web.hub import Hub

STATIC = Path(__file__).resolve().parent / "static"
REASONS = {200: "OK", 202: "Accepted", 400: "Bad Request", 403: "Forbidden", 404: "Not Found",
           409: "Conflict", 500: "Internal Server Error"}


async def _respond(w: asyncio.StreamWriter, code: int, body: bytes | dict | list, ctype: str = "application/json"
                   ) -> None:
    if not isinstance(body, bytes):
        body = json.dumps(body, default=str, ensure_ascii=False).encode()
    w.write(f"HTTP/1.1 {code} {REASONS.get(code, '')}\r\nContent-Type: {ctype}\r\nContent-Length: {len(body)}\r\n"
            f"Cache-Control: no-store\r\nConnection: close\r\n\r\n".encode() + body)
    await w.drain()


def _file(path: Path) -> tuple[bytes, str] | None:
    if path.is_file():
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        return path.read_bytes(), ctype
    return None


class Server:
    def __init__(self, app: DeskApp) -> None:
        self.app = app
        self._tasks: set[asyncio.Task[None]] = set()

    async def handle(self, r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        try:
            head = await r.readuntil(b"\r\n\r\n")
            lines = head.decode("latin-1").split("\r\n")
            method, target, _ = lines[0].split(" ", 2)
            headers = {k.lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:] if ln)}
            raw = await r.readexactly(int(headers.get("content-length", 0)))
            body: dict[str, Any] = json.loads(raw) if raw else {}
            await self.route(method, urlparse(target).path, body, w)
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        except json.JSONDecodeError:
            await _respond(w, 400, {"ok": False, "error": "invalid JSON"})
        except Exception as exc:  # noqa: BLE001
            with_suppress = _respond(w, 500, {"ok": False, "error": str(exc)})
            try:
                await with_suppress
            except ConnectionError:
                pass
        finally:
            w.close()

    async def route(self, method: str, path: str, b: dict[str, Any], w: asyncio.StreamWriter) -> None:
        app = self.app
        if path == "/events":
            return await self.stream(w)
        if method == "GET" and path == "/api/state":
            return await _respond(w, 200, app.state())
        if method == "GET" and path.startswith("/api/evidence/"):
            try:
                data, ctype = app.evidence_blob(unquote(path.removeprefix("/api/evidence/")))
            except Exception as exc:  # noqa: BLE001
                return await _respond(w, 404, {"ok": False, "error": str(exc)})
            return await _respond(w, 200, data, ctype)
        if method == "POST":
            try:
                result = await self.api(path, b)
            except ActionError as exc:
                return await _respond(w, 400, {"ok": False, "error": str(exc)})
            if result is None:
                return await _respond(w, 404, {"ok": False, "error": "unknown endpoint"})
            return await _respond(w, 202 if result.get("started") else 200, result)
        if method == "GET":
            rel = "index.html" if path == "/" else path.lstrip("/")
            full = (STATIC / rel).resolve()
            if STATIC in full.parents and (found := _file(full)):
                return await _respond(w, 200, *found)
        await _respond(w, 404, {"error": "not found"})

    async def api(self, path: str, b: dict[str, Any]) -> dict[str, Any] | None:
        app = self.app
        if path == "/api/patient/message":
            await app.patient_say(b.get("text", ""), b.get("features"), b.get("patient"))
        elif path == "/api/patient/attach-lab":
            await app.attach_lab(b.get("patient"))
        elif path == "/api/pacs/new-study":
            await app.new_study(b.get("patient"))
        elif path == "/api/reset":
            await app.reset()
        elif path == "/api/scenario/step":
            n = int(b.get("n", 0))
            app._patient(b.get("patient"))  # refuse an unknown patient before starting
            if app.running is not None:
                raise ActionError("a step is already running")
            self._spawn(app.run_step(n, b.get("patient")))
            return {"ok": True, "started": n}
        elif path == "/api/scenario/autoplay":
            if app.running is not None:
                raise ActionError("already running")
            app._patient(b.get("patient"))
            self._spawn(app.autoplay(b.get("patient")))
            return {"ok": True, "started": "autoplay"}
        elif path == "/api/board/act":
            kw = {k: v for k, v in b.items() if k not in ("item", "action", "clinician")}
            return {"ok": True, "result": await app.board_act(b.get("item", ""), b.get("action", ""),
                                                              b.get("clinician", ""), **kw)}
        else:
            return None
        return {"ok": True}

    def _spawn(self, coro: Any) -> None:
        async def run() -> None:
            try:
                await coro
            except Exception as exc:  # noqa: BLE001
                self.app.hub.publish("error", {"seat": "scenario", "message": str(exc)})

        t = asyncio.create_task(run())
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    async def stream(self, w: asyncio.StreamWriter) -> None:
        w.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nCache-Control: no-cache\r\n"
                b"Connection: keep-alive\r\n\r\nretry: 1500\r\n\r\n")
        q = self.app.hub.subscribe()
        try:
            while True:
                try:
                    w.write(Hub.sse(await asyncio.wait_for(q.get(), 15)))
                except TimeoutError:
                    w.write(b": ping\r\n\r\n")
                await w.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            self.app.hub.unsubscribe(q)


async def serve(app: DeskApp, host: str, port: int) -> asyncio.Server:
    return await asyncio.start_server(Server(app).handle, host, port)


async def amain(args: argparse.Namespace) -> None:
    app = await DeskApp.create(light=args.light)
    server = await serve(app, args.host, args.port)
    print(f"\n  Kidney Care Desk (Python)  →  http://{args.host}:{args.port}"
          f"{'   [light mode: no imaging / genomics / memory]' if args.light else ''}\n")
    async with server:
        await server.serve_forever()


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="kcd.web")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5173)
    ap.add_argument("--light", action="store_true", help="skip the Cortex-backed seats (no sqlalchemy needed)")
    try:
        asyncio.run(amain(ap.parse_args(argv)))
    except KeyboardInterrupt:
        pass
