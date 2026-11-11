"""NATS JetStream adapter for the A2A bus. Same semantics as ``InMemoryBus`` (see kcd_core.bus), made durable.

- publish: JetStream publish with a message id, so it returns only once the message is stored
- subscribe: a durable *pull* consumer per subject with a queue-less shared name, so any number of replicas of
  the same seat share the work and a restart resumes where it stopped; explicit acks; backpressure by design
- failures: a raising handler is nak'd with exponential backoff; after `max_attempts` (or on PermanentReject)
  the message goes to the **durable** dead-letter stream `dlq.<subject>`, and only then is the original
  terminated. If the dead-letter write itself fails the message is nak'd again: it is never silently lost
- crashes: the server redelivers unacked messages after `ack_wait`; a handler that outlives it keeps the
  message alive with in-progress heartbeats; a message whose workers keep crashing is dead-lettered on the
  extra delivery the server allows (`max_deliver = max_attempts + 1`)
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections.abc import Sequence

import nats
import nats.errors
import nats.js.errors
from nats.aio.client import Client as NatsClient
from nats.aio.msg import Msg
from nats.js import JetStreamContext
from nats.js.api import AckPolicy, ConsumerConfig, DeliverPolicy, RetentionPolicy, StorageType, StreamConfig

from ..bus import DeadLetter, Handler, PermanentReject, dead_letter
from ..contracts import Envelope
from .pg_outbox import msg_id_for

log = logging.getLogger(__name__)

_DAY = 24 * 3600


class NatsJetStreamBus:
    def __init__(
        self,
        nc: NatsClient,
        *,
        stream: str = "KCD_A2A",
        dlq_stream: str = "KCD_DLQ",
        subjects: Sequence[str] = ("a2a.>",),
        dlq_subjects: Sequence[str] = ("dlq.>",),
        max_attempts: int = 5,
        ack_wait_s: float = 30.0,
        base_backoff_s: float = 1.0,
        fetch_batch: int = 10,
        replicas: int = 1,
        max_age_s: float = 7 * _DAY,
        dlq_max_age_s: float = 30 * _DAY,
        duplicate_window_s: float = 600.0,
        owns_connection: bool = False,
    ) -> None:
        self._nc = nc
        self._js: JetStreamContext = nc.jetstream()
        self._stream, self._dlq_stream = stream, dlq_stream
        self._subjects, self._dlq_subjects = list(subjects), list(dlq_subjects)
        self._max_attempts, self._ack_wait, self._base_backoff = max_attempts, ack_wait_s, base_backoff_s
        self._fetch_batch, self._replicas = fetch_batch, replicas
        self._max_age, self._dlq_max_age, self._dup_window = max_age_s, dlq_max_age_s, duplicate_window_s
        self._owns = owns_connection
        self._ready = False
        self._ready_lock = asyncio.Lock()
        self._subs: list[JetStreamContext.PullSubscription] = []
        self._tasks: list[asyncio.Task[None]] = []
        # This process's view, for tests and diagnostics. The DLQ stream is the durable record.
        self.dead_letters: list[DeadLetter] = []

    @classmethod
    async def connect(cls, url: str, **kwargs: object) -> NatsJetStreamBus:
        # Fail fast if the broker is unreachable at start-up (the orchestrator restarts us);
        # once connected, reconnect forever.
        nc = await nats.connect(url, connect_timeout=5, max_reconnect_attempts=-1, reconnect_time_wait=1.0,
                                name="kcd-core")
        return cls(nc, owns_connection=True, **kwargs)  # type: ignore[arg-type]

    @property
    def connected(self) -> bool:
        return bool(self._nc.is_connected)

    # -- streams ----------------------------------------------------------------

    async def ensure_streams(self) -> None:
        async with self._ready_lock:
            if self._ready:
                return
            await self._upsert(StreamConfig(
                name=self._stream, subjects=self._subjects, storage=StorageType.FILE,
                retention=RetentionPolicy.LIMITS, max_age=self._max_age, num_replicas=self._replicas,
                duplicate_window=self._dup_window,
            ))
            await self._upsert(StreamConfig(
                name=self._dlq_stream, subjects=self._dlq_subjects, storage=StorageType.FILE,
                retention=RetentionPolicy.LIMITS, max_age=self._dlq_max_age, num_replicas=self._replicas,
            ))
            self._ready = True

    async def _upsert(self, config: StreamConfig) -> None:
        try:
            await self._js.stream_info(config.name or "")
        except nats.js.errors.NotFoundError:
            await self._js.add_stream(config)
            log.info("created stream %s", config.name)
        else:
            await self._js.update_stream(config)

    # -- publish ----------------------------------------------------------------

    async def publish(self, subject: str, env: Envelope) -> None:
        await self.publish_raw(subject, env.model_dump_json(by_alias=True).encode(), msg_id=msg_id_for(env, subject))

    async def publish_raw(self, subject: str, data: bytes, *, msg_id: str) -> None:
        await self.ensure_streams()
        await self._js.publish(subject, data, headers={"Nats-Msg-Id": msg_id}, timeout=5.0)

    # -- subscribe --------------------------------------------------------------

    async def subscribe(self, subject: str, handler: Handler) -> None:
        await self.ensure_streams()
        durable = "c_" + re.sub(r"[^A-Za-z0-9_-]", "_", subject)
        if any(t.get_name() == f"bus:{subject}" for t in self._tasks):
            raise RuntimeError(f"'{subject}' already has a consumer in this process")
        psub = await self._js.pull_subscribe(
            subject, durable=durable, stream=self._stream,
            config=ConsumerConfig(
                durable_name=durable, ack_policy=AckPolicy.EXPLICIT, deliver_policy=DeliverPolicy.ALL,
                ack_wait=self._ack_wait, max_deliver=self._max_attempts + 1, filter_subject=subject,
                max_ack_pending=self._fetch_batch * 4,
            ),
        )
        self._subs.append(psub)
        self._tasks.append(asyncio.create_task(self._pull_loop(psub, subject, handler), name=f"bus:{subject}"))

    async def _pull_loop(self, psub: JetStreamContext.PullSubscription, subject: str, handler: Handler) -> None:
        while True:
            try:
                msgs = await psub.fetch(self._fetch_batch, timeout=1.0)
            except (nats.errors.TimeoutError, TimeoutError):
                continue
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("fetch failed on %s; retrying", subject)
                await asyncio.sleep(1.0)
                continue
            for msg in msgs:
                await self._process(msg, subject, handler)

    async def _process(self, msg: Msg, subject: str, handler: Handler) -> None:
        attempt = msg.metadata.num_delivered
        try:
            env = Envelope.model_validate_json(msg.data)
        except Exception as exc:  # noqa: BLE001
            await self._dead_letter(msg, subject, None, attempt, f"undecodable: {exc!r}")
            return
        if attempt > self._max_attempts:
            await self._dead_letter(msg, subject, env, attempt - 1, "max deliveries exceeded (worker crashed?)")
            return
        heartbeat = asyncio.create_task(self._keep_alive(msg))
        try:
            await handler(env)
        except PermanentReject as exc:
            await self._dead_letter(msg, subject, env, attempt, f"rejected: {exc}")
        except Exception as exc:  # noqa: BLE001 - any handler failure is a retry
            if attempt >= self._max_attempts:
                log.warning("dead-lettering %s on %s after %d attempts", env.id, subject, attempt)
                await self._dead_letter(msg, subject, env, attempt, repr(exc))
            else:
                delay = min(self._base_backoff * 2 ** (attempt - 1), 60.0)
                log.info("nak %s on %s attempt=%d delay=%.1fs: %r", env.id, subject, attempt, delay, exc)
                await msg.nak(delay=delay)
        else:
            await msg.ack()
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat

    async def _keep_alive(self, msg: Msg) -> None:
        """A slow handler (a seat may legitimately run for its whole timeout) must not be redelivered mid-flight."""
        while True:
            await asyncio.sleep(self._ack_wait / 3)
            with contextlib.suppress(Exception):
                await msg.in_progress()

    async def _dead_letter(self, msg: Msg, subject: str, env: Envelope | None, attempts: int, error: str) -> None:
        try:
            await self._js.publish(
                dead_letter(subject), msg.data, timeout=5.0,
                headers={"KCD-Error": error[:500], "KCD-Attempts": str(attempts), "KCD-Original-Subject": subject},
            )
        except Exception:
            # Cannot park it durably: do not terminate. The server redelivers and we try again.
            log.critical("dead-letter write failed for a message on %s (%s); will retry", subject, error, exc_info=True)
            await msg.nak(delay=5.0)
            return
        if env is not None:
            self.dead_letters.append(DeadLetter(subject, env, attempts, error))
        await msg.term()

    # -- lifecycle --------------------------------------------------------------

    async def close(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()
        for sub in self._subs:
            with contextlib.suppress(Exception):
                await sub.unsubscribe()
        self._subs.clear()
        if self._owns and self._nc.is_connected:
            with contextlib.suppress(Exception):
                await self._nc.drain()
