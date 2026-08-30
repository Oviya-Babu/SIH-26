"""S3-compatible object storage for document originals (CLAUDE.md §32, §38).

Document originals ARE the medical record (§17.3), so they are stored, not
transient. Encryption at rest is KMS/Vault-managed and enforced by bucket policy
rather than by this client — a client-side flag would be a suggestion, while a
bucket policy is a control.
"""

from __future__ import annotations

from typing import Any

import aioboto3
from botocore.config import Config

from medikiosk.config import Settings
from medikiosk.errors import DependencyUnavailable
from medikiosk.observability.logging_setup import get_logger

log = get_logger(__name__)


class ObjectStore:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._session = aioboto3.Session()
        self._config = Config(
            signature_version="s3v4",
            retries={"max_attempts": 3, "mode": "standard"},
            connect_timeout=5,
            read_timeout=30,
        )

    def _client(self):
        return self._session.client(
            "s3",
            endpoint_url=self._settings.s3_endpoint,
            aws_access_key_id=self._settings.s3_access_key,
            aws_secret_access_key=self._settings.s3_secret_key,
            region_name=self._settings.s3_region,
            config=self._config,
        )

    async def ensure_bucket(self) -> None:
        async with self._client() as s3:
            try:
                await s3.head_bucket(Bucket=self._settings.s3_bucket)
            except Exception:  # noqa: BLE001 — bucket absent or unreachable
                try:
                    await s3.create_bucket(Bucket=self._settings.s3_bucket)
                    log.info("object_bucket_created", component="object_store")
                except Exception as exc:  # noqa: BLE001
                    raise DependencyUnavailable(
                        "object storage is unavailable",
                        reason_code="object_store_unavailable",
                    ) from exc

    async def put(self, key: str, content: bytes, mime: str) -> None:
        async with self._client() as s3:
            try:
                await s3.put_object(
                    Bucket=self._settings.s3_bucket,
                    Key=key,
                    Body=content,
                    ContentType=mime,
                    # Server-side encryption; the KMS key is bucket policy.
                    ServerSideEncryption="AES256",
                )
            except Exception as exc:  # noqa: BLE001
                raise DependencyUnavailable(
                    "could not store document", reason_code="object_store_unavailable"
                ) from exc

    async def get(self, key: str) -> bytes:
        async with self._client() as s3:
            try:
                response = await s3.get_object(Bucket=self._settings.s3_bucket, Key=key)
                return await response["Body"].read()
            except Exception as exc:  # noqa: BLE001
                raise DependencyUnavailable(
                    "could not read document", reason_code="object_store_unavailable"
                ) from exc

    async def delete(self, key: str) -> None:
        async with self._client() as s3:
            try:
                await s3.delete_object(Bucket=self._settings.s3_bucket, Key=key)
            except Exception:  # noqa: BLE001
                log.warning("object_delete_failed", component="object_store")

    async def presigned_get(self, key: str, *, expires_seconds: int = 300) -> str:
        """Short-lived URL for the physician's document viewer.

        Five minutes, not hours: the viewer refetches, and a leaked URL should
        stop working before it can be shared.
        """
        async with self._client() as s3:
            return await s3.generate_presigned_url(
                "get_object",
                Params={"Bucket": self._settings.s3_bucket, "Key": key},
                ExpiresIn=expires_seconds,
            )

    async def health(self) -> dict[str, Any]:
        try:
            async with self._client() as s3:
                await s3.head_bucket(Bucket=self._settings.s3_bucket)
            return {"ok": True}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": type(exc).__name__}
