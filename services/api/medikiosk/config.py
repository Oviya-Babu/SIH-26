"""Runtime configuration.

[RED LINE §32] Secrets live in Vault or a KMS-backed store — never in a
committed env file. Everything here reads from the process environment, which in
production is populated by the secrets store, not by a file in the repository.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MEDIKIOSK_",
        env_file=None,  # deliberate: no .env loading in the app process (§32)
        extra="ignore",
    )

    environment: Literal["local", "ci", "staging", "production"] = "local"
    service_name: str = "medikiosk-api"
    api_root_path: str = ""

    # --- data tier -----------------------------------------------------------
    database_url: str = "postgresql://medikiosk_app:medikiosk_app@localhost:5432/medikiosk"
    database_pool_min: int = 2
    database_pool_max: int = 16
    database_statement_timeout_ms: int = 15_000

    redis_url: str = "redis://localhost:6379/0"
    rabbitmq_url: str = "amqp://medikiosk:devonly_change_me@localhost:5672/"

    # --- object storage ------------------------------------------------------
    s3_endpoint: str = "http://localhost:9000"
    s3_bucket: str = "medikiosk-documents"
    s3_access_key: str = "medikiosk"
    s3_secret_key: str = "devonly_change_me"
    s3_region: str = "ap-south-1"

    # --- identity / authorization -------------------------------------------
    oidc_issuer: str = "http://localhost:8080/realms/medikiosk"
    oidc_audience: str = "medikiosk-staff"
    oidc_jwks_ttl_seconds: int = 300
    # Roles that must present an MFA-satisfied token (§27).
    mfa_required_roles: tuple[str, ...] = (
        "physician",
        "ayush_practitioner",
        "clinical_admin",
        "it_admin",
        "security_officer",
    )

    opa_url: str = "http://localhost:8181"
    opa_decision_path: str = "medikiosk/authz/allow"
    opa_timeout_seconds: float = 1.0
    # [RED LINE §5.1] deny by default. If OPA is unreachable we refuse, we never
    # fall through to "allow" — an authorization outage is not an access grant.
    opa_fail_open: bool = False

    # Signing key for the ephemeral kiosk/patient and upload tokens. Injected
    # from the secrets store; a weak default is rejected outside local/ci.
    session_token_secret: str = "local-dev-only-not-a-secret"
    session_token_ttl_seconds: int = 3600
    upload_token_ttl_seconds: int = 2700  # ~45 min (§9)

    # --- AI gateway ----------------------------------------------------------
    ai_gateway_url: str = "http://localhost:8100"
    ai_asr_timeout_seconds: float = 5.0
    ai_nlu_timeout_seconds: float = 1.5
    ai_llm_timeout_seconds: float = 8.0  # §54 bounded async budget
    ai_ocr_timeout_seconds: float = 120.0

    # --- observability -------------------------------------------------------
    otlp_endpoint: str | None = None
    log_level: str = "INFO"
    # Emergency switch only; never enabled outside a synthetic-data environment.
    allow_unredacted_logs: bool = False

    # --- clinical safety -----------------------------------------------------
    # [RED LINE §53] placeholders until calibrated on real pilot data. They are
    # named as such so no report can present them as final numbers.
    confidence_tau_high_placeholder: float = Field(default=0.85, ge=0, le=1)
    confidence_tau_low_placeholder: float = Field(default=0.55, ge=0, le=1)
    # Confidence gate for document extraction auto-acceptance (§17.2).
    extraction_auto_accept_threshold: float = Field(default=0.90, ge=0, le=1)

    red_flag_ruleset_version: str = "emergency_v1"
    red_flag_sla_escalation_grace_seconds: int = 60

    # --- integrations (§23, §25) --------------------------------------------
    abdm_environment: Literal["sandbox", "production"] = "sandbox"
    abdm_base_url: str = "https://dev.abdm.gov.in"
    abdm_client_id: str | None = None
    abdm_client_secret: str | None = None
    abdm_consent_manager_id: str = "sbx"

    his_adapter_mode: Literal["mock", "live"] = "mock"
    his_base_url: str | None = None

    terminology_snapshot_version: str = "namaste-snapshot-2025.1"

    clamav_host: str = "localhost"
    clamav_port: int = 3310
    # A scan cannot be skipped; if the scanner is unreachable the document is
    # held in 'scanning', never passed through to OCR (§35 [RED LINE]).
    clamav_required: bool = True

    max_upload_bytes: int = 15 * 1024 * 1024
    allowed_upload_mime: tuple[str, ...] = ("image/jpeg", "image/png", "application/pdf")

    cors_allow_origins: tuple[str, ...] = (
        "http://localhost:3100",
        "http://localhost:3200",
    )

    @field_validator("session_token_secret")
    @classmethod
    def _reject_default_secret_outside_dev(cls, v: str, info) -> str:
        env = (info.data or {}).get("environment", "local")
        if env in ("staging", "production") and v == "local-dev-only-not-a-secret":
            raise ValueError(
                "session_token_secret must be provided from the secrets store "
                "outside local/ci (CLAUDE.md §32)"
            )
        return v

    @property
    def is_synthetic_data_environment(self) -> bool:
        """Dev/staging are synthetic-data-only (§28 [RED LINE])."""
        return self.environment in ("local", "ci", "staging")


@lru_cache
def get_settings() -> Settings:
    return Settings()
