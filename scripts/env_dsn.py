#!/usr/bin/env python3
"""Build connection strings from ``.env`` safely.

Two problems this solves, both of which bite the moment a password contains a
character that is special somewhere:

* ``source .env`` in a shell breaks on ``&``, ``%``, ``$`` and ``#``. Strong
  passwords contain exactly those characters, so the shell is the wrong parser.
* A password must be PERCENT-ENCODED inside a URL. ``p@ss%word`` in a DSN is not
  a password containing ``%`` — it is a malformed URL, and the failure surfaces
  as a confusing authentication error rather than a parse error.

    eval "$(python scripts/env_dsn.py --export)"
    python scripts/env_dsn.py owner       # print one DSN
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from urllib.parse import quote

REPO_ROOT = Path(__file__).resolve().parents[1]

DEFAULTS: dict[str, str] = {
    "POSTGRES_DB": "medikiosk",
    "POSTGRES_SUPERUSER": "medikiosk_owner",
    "POSTGRES_PASSWORD": "devonly_change_me",
    "POSTGRES_PORT": "5432",
    "APP_DB_PASSWORD": "medikiosk_app",
    "RELAY_DB_PASSWORD": "medikiosk_relay",
    "REDIS_PORT": "6379",
    "RABBITMQ_USER": "medikiosk",
    "RABBITMQ_PASSWORD": "devonly_change_me",
    "RABBITMQ_PORT": "5672",
    "KEYCLOAK_PORT": "8080",
    "OPA_PORT": "8181",
    "MINIO_ROOT_USER": "medikiosk",
    "MINIO_ROOT_PASSWORD": "devonly_change_me",
    "MINIO_PORT": "9000",
    "API_PORT": "8000",
    "AI_GATEWAY_PORT": "8100",
    "CLAMAV_PORT": "3310",
    "HOST": "127.0.0.1",
}


def read_env(path: Path) -> dict[str, str]:
    """Parse a dotenv file without a shell, using COMPOSE's semantics.

    No command substitution and no variable expansion, but ``$$`` IS unescaped to
    a literal ``$`` — because that is what Docker Compose does, and this helper
    must produce the same value the container actually received. Reading the file
    "literally" would hand back the escaped form and fail authentication against
    a correctly-configured database, which is a confusing way to be wrong.
    """
    values = dict(DEFAULTS)
    if not path.is_file():
        return values

    for raw_line in path.read_text("utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        value = value.replace("$$", "$")
        if key:
            values[key] = value
    return values


def dsns(env: dict[str, str]) -> dict[str, str]:
    host = env["HOST"]
    db = env["POSTGRES_DB"]
    pg_port = env["POSTGRES_PORT"]

    def pg(user: str, password: str) -> str:
        # safe="" so EVERY reserved character is encoded, including '/' and ':'.
        return (
            f"postgresql://{quote(user, safe='')}:{quote(password, safe='')}"
            f"@{host}:{pg_port}/{db}"
        )

    return {
        "owner": pg(env["POSTGRES_SUPERUSER"], env["POSTGRES_PASSWORD"]),
        "app": pg("medikiosk_app", env["APP_DB_PASSWORD"]),
        "relay": pg("medikiosk_relay", env["RELAY_DB_PASSWORD"]),
        "redis": f"redis://{host}:{env['REDIS_PORT']}/0",
        "rabbitmq": (
            f"amqp://{quote(env['RABBITMQ_USER'], safe='')}:"
            f"{quote(env['RABBITMQ_PASSWORD'], safe='')}@{host}:{env['RABBITMQ_PORT']}/"
        ),
        "opa": f"http://{host}:{env['OPA_PORT']}",
        "oidc": f"http://{host}:{env['KEYCLOAK_PORT']}/realms/medikiosk",
        "keycloak": f"http://{host}:{env['KEYCLOAK_PORT']}",
        "s3": f"http://{host}:{env['MINIO_PORT']}",
        "api": f"http://{host}:{env['API_PORT']}",
        "ai_gateway": f"http://{host}:{env['AI_GATEWAY_PORT']}",
    }


def export_block(env: dict[str, str], resolved: dict[str, str]) -> str:
    """Emit shell exports with single-quote escaping, safe to ``eval``."""

    def q(value: str) -> str:
        return "'" + value.replace("'", "'\\''") + "'"

    lines = [
        f"export MEDIKIOSK_MIGRATION_DSN={q(resolved['owner'])}",
        f"export MEDIKIOSK_DATABASE_URL={q(resolved['app'])}",
        f"export MEDIKIOSK_RELAY_DATABASE_URL={q(resolved['relay'])}",
        f"export MEDIKIOSK_TEST_DATABASE_URL={q(resolved['app'])}",
        f"export MEDIKIOSK_TEST_OWNER_DATABASE_URL={q(resolved['owner'])}",
        f"export MEDIKIOSK_REDIS_URL={q(resolved['redis'])}",
        f"export MEDIKIOSK_RABBITMQ_URL={q(resolved['rabbitmq'])}",
        f"export MEDIKIOSK_OPA_URL={q(resolved['opa'])}",
        f"export MEDIKIOSK_OIDC_ISSUER={q(resolved['oidc'])}",
        f"export MEDIKIOSK_S3_ENDPOINT={q(resolved['s3'])}",
        f"export MEDIKIOSK_S3_ACCESS_KEY={q(env['MINIO_ROOT_USER'])}",
        f"export MEDIKIOSK_S3_SECRET_KEY={q(env['MINIO_ROOT_PASSWORD'])}",
        f"export MEDIKIOSK_AI_GATEWAY_URL={q(resolved['ai_gateway'])}",
        f"export MEDIKIOSK_CLAMAV_HOST={q(env['HOST'])}",
        f"export MEDIKIOSK_CLAMAV_PORT={q(env['CLAMAV_PORT'])}",
        f"export KEYCLOAK_BASE_URL={q(resolved['keycloak'])}",
        f"export API_BASE_URL={q(resolved['api'])}",
        "export MEDIKIOSK_ENVIRONMENT=local",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("name", nargs="?", help="which DSN to print")
    parser.add_argument("--export", action="store_true", help="emit shell exports")
    parser.add_argument("--env-file", default=str(REPO_ROOT / ".env"))
    parser.add_argument("--host", default=None, help="override the host")
    args = parser.parse_args()

    env = read_env(Path(args.env_file))
    if args.host:
        env["HOST"] = args.host
    resolved = dsns(env)

    if args.export:
        print(export_block(env, resolved))
        return 0
    if args.name:
        if args.name not in resolved:
            print(
                f"unknown name {args.name!r}; choose from {', '.join(sorted(resolved))}",
                file=sys.stderr,
            )
            return 2
        print(resolved[args.name])
        return 0

    for name in sorted(resolved):
        print(f"{name:12} {resolved[name]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
