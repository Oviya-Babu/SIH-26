"""RabbitMQ publisher (CLAUDE.md §50).

The rule for using the broker at all: a workload qualifies only if it is
genuinely long-running, retryable, or fan-out. Three workloads qualify —
document processing, notifications, and integration relay.

[RED LINE §50] The interactive patient loop never touches this module. That is
checked by a test that asserts no module under ``modules/session`` or
``modules/clinical_protocol`` imports it.

Publishing is best-effort by design: the durable record of intent is the
transactional outbox row or the ``document.processing_status`` column, both
written in the caller's transaction. A publish failure therefore delays work; it
never loses it, and the relay/sweeper picks it up.
"""

from __future__ import annotations

from typing import Any

import aio_pika

from medikiosk.config import Settings
from medikiosk.db import as_json
from medikiosk.observability.logging_setup import get_logger

log = get_logger(__name__)

EXCHANGE = "medikiosk.events"

# routing key → (queue name, purpose)
QUEUES: dict[str, tuple[str, str]] = {
    "document.uploaded": ("medikiosk.document_processing", "OCR + entity extraction"),
    "integration.approved": ("medikiosk.integration_relay", "FHIR/ABDM/HIS export"),
    "notification.staff": ("medikiosk.notification", "non-urgent staff notification"),
}
DEAD_LETTER_EXCHANGE = "medikiosk.events.dlx"
DEAD_LETTER_QUEUE = "medikiosk.dead_letter"


class Broker:
    def __init__(self, settings: Settings) -> None:
        self._url = settings.rabbitmq_url
        self._connection: aio_pika.abc.AbstractRobustConnection | None = None
        self._channel: aio_pika.abc.AbstractChannel | None = None
        self._exchange: aio_pika.abc.AbstractExchange | None = None
        self.available = False

    async def connect(self) -> bool:
        """Connect and declare topology. Returns False if unavailable.

        Startup does NOT fail on a broker outage: §37 requires documents to queue
        visibly and the physician workflow to keep working. Refusing to start
        would turn a degraded feature into a total outage.
        """
        try:
            self._connection = await aio_pika.connect_robust(self._url, timeout=5)
            self._channel = await self._connection.channel(publisher_confirms=True)
            await self._channel.set_qos(prefetch_count=8)

            dlx = await self._channel.declare_exchange(
                DEAD_LETTER_EXCHANGE, aio_pika.ExchangeType.FANOUT, durable=True
            )
            dlq = await self._channel.declare_queue(DEAD_LETTER_QUEUE, durable=True)
            await dlq.bind(dlx)

            self._exchange = await self._channel.declare_exchange(
                EXCHANGE, aio_pika.ExchangeType.TOPIC, durable=True
            )
            for routing_key, (queue_name, _) in QUEUES.items():
                queue = await self._channel.declare_queue(
                    queue_name,
                    durable=True,
                    arguments={"x-dead-letter-exchange": DEAD_LETTER_EXCHANGE},
                )
                await queue.bind(self._exchange, routing_key=routing_key)

            self.available = True
            log.info("broker_ready", component="broker", count=len(QUEUES))
        except Exception as exc:  # noqa: BLE001
            self.available = False
            log.warning(
                "broker_unavailable",
                component="broker",
                error_class=type(exc).__name__,
                fallback_engaged=True,
            )
        return self.available

    async def close(self) -> None:
        if self._connection is not None:
            await self._connection.close()
        self.available = False

    async def publish(
        self,
        routing_key: str,
        payload: dict[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> bool:
        """Publish one event. Returns False when the broker is unavailable.

        The caller must already have persisted the intent. A False return is a
        delay, never a loss.
        """
        if not self.available or self._exchange is None:
            return False
        try:
            message = aio_pika.Message(
                body=as_json(payload).encode("utf-8"),
                content_type="application/json",
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                message_id=idempotency_key,
                headers={"idempotency_key": idempotency_key} if idempotency_key else None,
            )
            await self._exchange.publish(message, routing_key=routing_key)
            return True
        except Exception as exc:  # noqa: BLE001
            self.available = False
            log.warning(
                "broker_publish_failed",
                component="broker",
                error_class=type(exc).__name__,
                reason_code=routing_key,
            )
            return False

    async def queue_depths(self) -> dict[str, int]:
        """Depths for the dead-letter-growth alert of §39."""
        if not self.available or self._channel is None:
            return {}
        depths: dict[str, int] = {}
        for _, (queue_name, _) in QUEUES.items():
            try:
                queue = await self._channel.declare_queue(queue_name, durable=True, passive=True)
                depths[queue_name] = queue.declaration_result.message_count or 0
            except Exception:  # noqa: BLE001
                continue
        try:
            dlq = await self._channel.declare_queue(
                DEAD_LETTER_QUEUE, durable=True, passive=True
            )
            depths[DEAD_LETTER_QUEUE] = dlq.declaration_result.message_count or 0
        except Exception:  # noqa: BLE001
            pass
        return depths
