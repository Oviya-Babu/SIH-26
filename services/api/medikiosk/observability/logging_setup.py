"""Structured logging, redacted from the first log line (CLAUDE.md §57 Phase 0).

Every log record in this process passes through :func:`redact_event`. There is
no second logger, no "debug logger" bypass, and no code path that formats a
record without the processor chain.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

from medikiosk.config import Settings
from medikiosk.observability.redaction import Pseudonymiser, redact_event

_configured = False


def _redaction_processor(pseudonymiser: Pseudonymiser, strict: bool):
    def processor(_logger: Any, _name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
        return redact_event(event_dict, pseudonymiser=pseudonymiser, strict=strict)

    return processor


def _context_processor(settings: Settings):
    def processor(_logger: Any, _name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
        event_dict.setdefault("service", settings.service_name)
        event_dict.setdefault("environment", settings.environment)
        return event_dict

    return processor


def configure_logging(settings: Settings) -> None:
    global _configured
    if _configured:
        return

    if settings.allow_unredacted_logs and not settings.is_synthetic_data_environment:
        raise RuntimeError(
            "allow_unredacted_logs is forbidden outside a synthetic-data "
            "environment (CLAUDE.md §28 [RED LINE])"
        )

    pseudonymiser = Pseudonymiser(settings.session_token_secret)
    # Strict mode turns a PHI-logging bug into a test failure rather than a
    # silent privacy incident. Production drops instead of crashing.
    strict = settings.is_synthetic_data_environment

    processors = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _context_processor(settings),
        structlog.processors.format_exc_info,
        # Redaction is the LAST processor before rendering: nothing added after
        # it can escape the allowlist.
        _redaction_processor(pseudonymiser, strict),
        structlog.processors.JSONRenderer(),
    ]

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping().get(settings.log_level.upper(), logging.INFO)
        ),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )

    # Route stdlib logging (uvicorn, asyncpg, httpx) through the same chain so a
    # third-party library cannot emit an unredacted line.
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=[
                structlog.processors.add_log_level,
                structlog.processors.TimeStamper(fmt="iso", utc=True),
            ],
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                _context_processor(settings),
                _redaction_processor(pseudonymiser, strict=False),
                structlog.processors.JSONRenderer(),
            ],
        )
    )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(settings.log_level.upper())
    for noisy in ("uvicorn.access", "uvicorn.error", "asyncpg", "httpx", "httpcore"):
        logging.getLogger(noisy).handlers = [handler]
        logging.getLogger(noisy).propagate = False

    _configured = True


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)


def reset_logging_for_tests() -> None:
    global _configured
    _configured = False
