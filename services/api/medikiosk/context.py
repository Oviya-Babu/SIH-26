"""Application context — the single place dependencies are wired.

Constructing everything here, once, is what makes the isolation rules of §20
checkable: the AI Gateway client is an HTTP client and nothing else, and the
database handle is never handed to it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import httpx

from medikiosk.ai.gateway_client import AIGatewayClient
from medikiosk.modules.abdm import AbdmSandboxClient
from medikiosk.config import Settings
from medikiosk.db import Database
from medikiosk.infrastructure.broker import Broker
from medikiosk.infrastructure.object_store import ObjectStore
from medikiosk.modules.ayush_namaste.service import TerminologyRegistry
from medikiosk.modules.clinical_protocol.engine import Thresholds
from medikiosk.modules.clinical_protocol.registry import ProtocolRegistry
from medikiosk.modules.document.security import MalwareScanner
from medikiosk.modules.localization.registry import LocalizationRegistry
from medikiosk.modules.purge.service import TransientStore
from medikiosk.modules.triage.red_flag_engine import RedFlagRegistry
from medikiosk.observability.logging_setup import get_logger
from medikiosk.security.oidc import OIDCVerifier
from medikiosk.security.opa import OPAClient
from medikiosk.security.tokens import TokenService

log = get_logger(__name__)


def default_content_root() -> Path:
    """Governed content lives at the repository root, shared with the frontends.

    One copy means the kiosk's UI strings and the API's question rendering can
    never drift, and a governance review covers both.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "content"
        if (candidate / "protocols").is_dir():
            return candidate
    raise RuntimeError("governed content directory not found")


@dataclass
class AppContext:
    settings: Settings
    db: Database
    http: httpx.AsyncClient
    oidc: OIDCVerifier
    opa: OPAClient
    tokens: TokenService
    protocols: ProtocolRegistry
    localization: LocalizationRegistry
    red_flags: RedFlagRegistry
    terminology: TerminologyRegistry
    thresholds: Thresholds
    content_root: Path
    ai: AIGatewayClient
    abdm: AbdmSandboxClient
    broker: Broker
    objects: ObjectStore
    transient_store: TransientStore
    scanner: MalwareScanner | None

    @classmethod
    async def create(cls, settings: Settings, *, connect_db: bool = True) -> AppContext:
        content_root = default_content_root()

        protocols = ProtocolRegistry(content_root)
        loaded = protocols.load_all()

        localization = LocalizationRegistry(content_root)
        localization.load_all()
        # An untranslated question must never reach a patient: refuse to start.
        localization.assert_complete(loaded)

        red_flags = RedFlagRegistry(content_root)
        ruleset = red_flags.load(settings.red_flag_ruleset_version)
        disarmed = red_flags.validate_against(ruleset, loaded)
        if disarmed:
            raise RuntimeError(
                "refusing to start: red-flag rules are disarmed or mis-declared: "
                + ", ".join(disarmed)
                + " (CLAUDE.md §14, §52)"
            )

        http = httpx.AsyncClient(
            timeout=httpx.Timeout(10.0),
            limits=httpx.Limits(max_connections=64, max_keepalive_connections=16),
        )

        # The terminology snapshot is validated at startup so a missing or empty
        # snapshot is a startup failure, not a surprise during an AYUSH consult.
        terminology = TerminologyRegistry(content_root)
        terminology.load(settings.terminology_snapshot_version)

        db = Database(settings)
        if connect_db:
            try:
                await db.connect()
            except Exception as exc:
                log.warning(
                    "database_unavailable_at_startup",
                    component="startup",
                    error=str(exc),
                    fallback_engaged=True,
                )

        broker = Broker(settings)
        objects = ObjectStore(settings)
        if connect_db:
            try:
                await broker.connect()
            except Exception as exc:
                log.warning("broker_unavailable_at_startup", error=str(exc))
            try:
                await objects.ensure_bucket()
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "object_store_unavailable_at_startup",
                    component="startup",
                    error_class=type(exc).__name__,
                    fallback_engaged=True,
                )

        scanner = (
            MalwareScanner(
                settings.clamav_host,
                settings.clamav_port,
                required=settings.clamav_required,
            )
            if settings.clamav_host
            else None
        )

        log.info(
            "app_context_ready",
            component="startup",
            count=len(loaded),
            ruleset_version=ruleset.version,
        )

        return cls(
            settings=settings,
            db=db,
            http=http,
            oidc=OIDCVerifier(settings, http),
            opa=OPAClient(settings, http),
            tokens=TokenService(settings.session_token_secret),
            protocols=protocols,
            localization=localization,
            red_flags=red_flags,
            terminology=terminology,
            thresholds=Thresholds(
                tau_high_placeholder=settings.confidence_tau_high_placeholder,
                tau_low_placeholder=settings.confidence_tau_low_placeholder,
            ),
            content_root=content_root,
            ai=AIGatewayClient(settings, http),
            abdm=AbdmSandboxClient(settings, http),
            broker=broker,
            objects=objects,
            transient_store=TransientStore(settings.redis_url),
            scanner=scanner,
        )

    async def aclose(self) -> None:
        await self.transient_store.close()
        await self.broker.close()
        await self.http.aclose()
        await self.db.close()

    @property
    def ruleset(self):
        return self.red_flags.load(self.settings.red_flag_ruleset_version)
