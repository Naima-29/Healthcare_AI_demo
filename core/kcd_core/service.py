"""The Core process: `python -m kcd_core.service`.

One process runs the dispatcher (consumes the bus, enforces routes, opens escalations/gates), the outbox relay,
the escalation worker, and a small HTTP server (health + control-plane API). Seats run in their own containers
and only share the bus and Postgres with this process.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import uvicorn
from fastapi import FastAPI

from .adapters._pg import make_pool
from .adapters.nats_bus import NatsJetStreamBus
from .adapters.pg_outbox import OutboxRelay, PostgresOutbox
from .adapters.pg_stores import (
    PostgresAuditSink,
    PostgresEscalationStore,
    PostgresGateQueue,
    PostgresIdempotencyStore,
    PostgresOnCallRota,
)
from .api import build_authenticator, build_router, install_error_handlers
from .chief_of_staff.llm import DraftModel, OpenAICompatDraftModel
from .clock import SystemClock
from .contracts import PolicyCatalog
from .control_plane import ControlPlane, ManifestReloader, PostgresManifestStore
from .dispatcher import Dispatcher
from .escalation import EscalationService, Pager
from .keys import load_keyring
from .migrate import apply_migrations
from .pagers import LogPager, WebhookPager
from .registry import ManifestRegistry
from .settings import Settings
from .workers import escalation_worker, run_periodically

log = logging.getLogger("kcd_core")


def build_pager(settings: Settings) -> Pager:
    if settings.pager == "webhook":
        assert settings.pager_url and settings.pager_secret  # enforced by Settings.validate_for_env
        return WebhookPager(settings.pager_url, settings.pager_secret)
    log.warning("KCD_PAGER=log: escalations are logged and reach NOBODY (fine for dev/test only)")
    return LogPager()


@dataclass
class Core:
    settings: Settings
    pool: Any
    bus: NatsJetStreamBus
    registry: ManifestRegistry
    audit: PostgresAuditSink
    dispatcher: Dispatcher
    escalations: EscalationService
    outbox: PostgresOutbox
    relay: OutboxRelay
    rota: PostgresOnCallRota
    gates: PostgresGateQueue
    pager: Pager
    plane: ControlPlane
    reloader: ManifestReloader
    extras: list[Any] = field(default_factory=list)

    async def start(self) -> None:
        await self.bus.ensure_streams()
        await self.dispatcher.start()

    async def ready(self) -> dict[str, Any]:
        def db() -> bool:
            with self.pool.connection() as conn:
                return conn.execute("SELECT 1").fetchone() is not None

        db_ok = False
        with contextlib.suppress(Exception):
            db_ok = await asyncio.to_thread(db)
        return {
            "database": db_ok, "bus": self.bus.connected,
            "outbox_pending": await asyncio.to_thread(self.outbox.pending) if db_ok else None,
            "outbox_dead": await asyncio.to_thread(self.outbox.dead) if db_ok else None,
        }


async def build_core(settings: Settings) -> Core:
    settings.validate_for_env()
    if settings.migrate_on_start:
        applied = await asyncio.to_thread(
            apply_migrations, settings.database_url, settings.migrations_dir, options=settings.pg_options
        )
        log.info("migrations applied: %s", applied or "none pending")
    pool = await asyncio.to_thread(
        make_pool, settings.database_url, max_size=settings.pg_pool_max, name="kcd-core", options=settings.pg_options
    )
    catalog = PolicyCatalog.load(settings.catalog_path)
    registry = ManifestRegistry.from_dir(catalog, settings.manifests_dir)
    bus = await NatsJetStreamBus.connect(
        settings.nats_url, max_attempts=settings.bus_max_attempts, replicas=settings.bus_replicas
    )
    audit, rota, gates = PostgresAuditSink(pool), PostgresOnCallRota(pool), PostgresGateQueue(pool)
    pager, clock = build_pager(settings), SystemClock()
    escalations = EscalationService(
        rota=rota, pager=pager, store=PostgresEscalationStore(pool), clock=clock, audit=audit,
        lease=timedelta(seconds=settings.escalation_lease_s),
    )
    outbox = PostgresOutbox(pool)
    dispatcher = Dispatcher(
        bus=bus, registry=registry, seat_keys=load_keyring(settings.keys_dir), audit=audit, clock=clock,
        escalations=escalations, gates=gates, idempotency=PostgresIdempotencyStore(pool), router=outbox,
        default_unit=settings.default_unit,
    )
    store = PostgresManifestStore(pool)
    draft_model: DraftModel | None = None
    if settings.llm_base_url and settings.llm_model:
        draft_model = OpenAICompatDraftModel(settings.llm_base_url, settings.llm_model, api_key=settings.llm_api_key)
    plane = ControlPlane(registry=registry, store=store, audit=audit, clock=clock, model=draft_model)
    reloader = ManifestReloader(registry, store)
    await asyncio.to_thread(reloader.sync_once)  # manifests approved earlier win over the files baked into the image
    return Core(settings, pool, bus, registry, audit, dispatcher, escalations, outbox,
                OutboxRelay(pool, bus), rota, gates, pager, plane, reloader)


def build_app(core: Core) -> FastAPI:
    app = FastAPI(title="KCD Core", docs_url=None, redoc_url=None)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:  # liveness: the process is up and the loop is responsive
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> Any:  # readiness: dependencies reachable
        from fastapi.responses import JSONResponse

        state = await core.ready()
        return JSONResponse(state, status_code=200 if state["database"] and state["bus"] else 503)

    app.include_router(build_router(core.plane, build_authenticator(core.settings)))
    install_error_handlers(app)
    return app


async def run(settings: Settings | None = None) -> None:
    settings = settings or Settings()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    core = await build_core(settings)
    await core.start()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    server = uvicorn.Server(uvicorn.Config(build_app(core), host=settings.http_host, port=settings.http_port,
                                           log_level="warning"))
    server.capture_signals = contextlib.nullcontext  # type: ignore[method-assign,assignment]  # we own shutdown
    tasks = [
        asyncio.create_task(core.relay.run(stop), name="outbox-relay"),
        asyncio.create_task(
            escalation_worker(core.escalations, stop, settings.escalation_interval_s), name="escalation"
        ),
        asyncio.create_task(
            run_periodically("housekeeping", lambda: asyncio.to_thread(_housekeeping, core), 3600, stop),
            name="housekeeping",
        ),
        asyncio.create_task(core.reloader.run(stop), name="manifest-reload"),
        asyncio.create_task(server.serve(), name="http"),
    ]
    log.info("core up (env=%s, pager=%s)", settings.env, settings.pager)
    await stop.wait()
    log.info("shutting down")
    server.should_exit = True
    for t in tasks:
        with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(t, 10)
    await core.bus.close()
    await asyncio.to_thread(core.pool.close)


def _housekeeping(core: Core) -> None:
    from datetime import timedelta

    core.outbox.prune_sent(timedelta(days=7))
    PostgresIdempotencyStore(core.pool).prune(timedelta(days=30))


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
