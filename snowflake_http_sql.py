#!/usr/bin/env python3
"""
Snowflake SQL API v2 over **plain HTTPS** (no snowflake-connector-python).

Auth (pick one):
  A) Key-pair JWT (service-style): RSA private key PEM + SNOWFLAKE_ACCOUNT + SNOWFLAKE_USER
  B) Bearer token you already have: OAuth access token or programmatic access token (PAT)

Install:
  pip install requests pyjwt cryptography

Optional (load env from file):
  pip install python-dotenv
  Copy ``snowflake.env.example`` values into ``.env.snowflake`` next to this script, or export vars.

Run SQL from a file:
  python snowflake_http_sql.py --sql-file sql/jira_dna_ae_stories.sql

Docs:
  https://docs.snowflake.com/en/developer-guide/sql-api/sql-api-reference
  https://docs.snowflake.com/en/developer-guide/sql-api/authenticating
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import jwt
import requests
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

_ROOT = Path(__file__).resolve().parent


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    p = _ROOT / ".env.snowflake"
    if p.is_file():
        load_dotenv(p)
    load_dotenv(_ROOT / ".env")


def _normalize_jwt_account_identifier(account: str) -> str:
    """
    Snowflake JWT ``iss`` / ``sub`` use a shortened account id (see SQL API auth docs).
    - If locator looks like ``xy12345.us-east-1.aws``, keep the segment before the first ``.``.
    - ``*.global`` accounts: not handled specially here — set SNOWFLAKE_JWT_ACCOUNT explicitly.
    """
    account = account.strip()
    explicit = (os.environ.get("SNOWFLAKE_JWT_ACCOUNT") or "").strip()
    if explicit:
        return explicit.upper().replace(".", "-")
    if ".global" in account.casefold():
        return account.upper().replace(".", "-")
    if "." in account:
        account = account.split(".", 1)[0]
    return account.upper().replace(".", "-")


def _snowflake_host() -> str:
    host = (os.environ.get("SNOWFLAKE_HOST") or "").strip().rstrip("/")
    if host.casefold().startswith("https://"):
        host = host[8:]
    if host:
        return host
    acct = (os.environ.get("SNOWFLAKE_ACCOUNT") or "").strip().rstrip("/")
    if not acct:
        raise SystemExit("Set SNOWFLAKE_HOST (e.g. xy12345.us-east-1.aws.snowflakecomputing.com) "
                         "or SNOWFLAKE_ACCOUNT (e.g. xy12345.us-east-1.aws).")
    if ".snowflakecomputing.com" in acct.casefold():
        return acct.split("https://", 1)[-1].split("http://", 1)[-1].strip("/")
    return f"{acct}.snowflakecomputing.com"


def _public_key_fingerprint_sha256(private_key: Any) -> str:
    pub_der = private_key.public_key().public_bytes(
        Encoding.DER,
        PublicFormat.SubjectPublicKeyInfo,
    )
    digest = hashlib.sha256(pub_der).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii")


def _load_private_key(pem_path: Path, passphrase: str | None) -> Any:
    data = pem_path.read_bytes()
    pw = passphrase.encode("utf-8") if passphrase else None
    try:
        return serialization.load_pem_private_key(data, password=pw, backend=default_backend())
    except TypeError as e:
        raise SystemExit(
            "Private key appears encrypted. Set SNOWFLAKE_PRIVATE_KEY_PASSPHRASE "
            "or pass --private-key-passphrase."
        ) from e


def mint_keypair_jwt(
    *,
    account_for_jwt: str,
    user: str,
    private_key_path: Path,
    passphrase: str | None,
    lifetime_minutes: int = 59,
) -> str:
    """Build ``Authorization: Bearer`` JWT for KEYPAIR_JWT (RS256)."""
    private_key = _load_private_key(private_key_path, passphrase)
    fp = _public_key_fingerprint_sha256(private_key)
    user_u = user.strip().upper()
    qualified = f"{account_for_jwt}.{user_u}"
    now = datetime.now(timezone.utc)
    payload = {
        "iss": f"{qualified}.{fp}",
        "sub": qualified,
        "iat": now,
        "exp": now + timedelta(minutes=min(lifetime_minutes, 59)),
    }
    token = jwt.encode(payload, private_key, algorithm="RS256")
    if isinstance(token, bytes):
        return token.decode("ascii")
    return str(token)


def post_statement(
    *,
    host: str,
    bearer: str,
    token_type: str | None,
    statement: str,
    warehouse: str | None,
    database: str | None,
    schema: str | None,
    role: str | None,
    timeout: int,
) -> requests.Response:
    url = f"https://{host}/api/v2/statements"
    headers: dict[str, str] = {
        "Authorization": f"Bearer {bearer}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "dna_jira_release_notes/snowflake_http_sql.py",
    }
    if token_type:
        headers["X-Snowflake-Authorization-Token-Type"] = token_type
    body: dict[str, Any] = {
        "statement": statement,
        "timeout": timeout,
    }
    if warehouse:
        body["warehouse"] = warehouse
    if database:
        body["database"] = database
    if schema:
        body["schema"] = schema
    if role:
        body["role"] = role
    return requests.post(url, headers=headers, json=body, timeout=timeout + 30)


def main() -> None:
    _load_dotenv()
    p = argparse.ArgumentParser(description="Run SQL via Snowflake SQL API v2 (HTTPS only).")
    p.add_argument(
        "--sql",
        default=os.environ.get("SNOWFLAKE_STATEMENT") or "select current_version(), current_user(), current_role()",
        help="SQL to execute (default: lightweight identity check). Ignored if --sql-file is set.",
    )
    p.add_argument(
        "--sql-file",
        type=Path,
        default=Path(os.environ["SNOWFLAKE_SQL_FILE"]).expanduser()
        if (os.environ.get("SNOWFLAKE_SQL_FILE") or "").strip()
        else None,
        help="Read SQL from this UTF-8 file (overrides --sql). Same as env SNOWFLAKE_SQL_FILE.",
    )
    p.add_argument("--warehouse", default=os.environ.get("SNOWFLAKE_WAREHOUSE"))
    p.add_argument("--database", default=os.environ.get("SNOWFLAKE_DATABASE"))
    p.add_argument("--schema", default=os.environ.get("SNOWFLAKE_SCHEMA"))
    p.add_argument("--role", default=os.environ.get("SNOWFLAKE_ROLE"))
    p.add_argument("--timeout", type=int, default=int(os.environ.get("SNOWFLAKE_TIMEOUT") or "120"))
    p.add_argument(
        "--private-key",
        type=Path,
        default=Path(os.environ["SNOWFLAKE_PRIVATE_KEY_PATH"])
        if (os.environ.get("SNOWFLAKE_PRIVATE_KEY_PATH") or "").strip()
        else None,
        help="Path to RSA private key PEM (PKCS#8). Used with key-pair JWT unless --bearer-token set.",
    )
    p.add_argument(
        "--private-key-passphrase",
        default=os.environ.get("SNOWFLAKE_PRIVATE_KEY_PASSPHRASE") or "",
        help="Passphrase for encrypted PEM (optional).",
    )
    p.add_argument(
        "--bearer-token",
        default=(os.environ.get("SNOWFLAKE_OAUTH_ACCESS_TOKEN") or os.environ.get("SNOWFLAKE_PROGRAMMATIC_ACCESS_TOKEN") or "").strip(),
        help="Use an existing OAuth or programmatic access token (skip JWT minting).",
    )
    p.add_argument(
        "--bearer-token-type",
        choices=["", "OAUTH", "PROGRAMMATIC_ACCESS_TOKEN", "KEYPAIR_JWT"],
        default=(os.environ.get("SNOWFLAKE_BEARER_TOKEN_TYPE") or "").strip().upper() or "",
        help="Value for X-Snowflake-Authorization-Token-Type when using --bearer-token.",
    )
    args = p.parse_args()

    if args.sql_file:
        statement = args.sql_file.expanduser().resolve().read_text(encoding="utf-8").strip()
        if not statement:
            raise SystemExit(f"Empty SQL file: {args.sql_file}")
    else:
        statement = (args.sql or "").strip()
        if not statement:
            raise SystemExit("Empty --sql / SNOWFLAKE_STATEMENT.")

    host = _snowflake_host()
    token_type: str | None = None
    bearer: str

    if args.bearer_token:
        bearer = args.bearer_token
        if args.bearer_token_type:
            token_type = args.bearer_token_type
    else:
        account_raw = (os.environ.get("SNOWFLAKE_ACCOUNT") or "").strip()
        user = (os.environ.get("SNOWFLAKE_USER") or "").strip()
        if not account_raw or not user:
            raise SystemExit(
                "For key-pair JWT: set SNOWFLAKE_ACCOUNT, SNOWFLAKE_USER, SNOWFLAKE_PRIVATE_KEY_PATH "
                "(or pass --bearer-token with OAuth / programmatic token)."
            )
        if not args.private_key:
            raise SystemExit("Missing private key: set SNOWFLAKE_PRIVATE_KEY_PATH or pass --private-key.")
        acct_jwt = _normalize_jwt_account_identifier(account_raw)
        passphrase = args.private_key_passphrase or None
        bearer = mint_keypair_jwt(
            account_for_jwt=acct_jwt,
            user=user,
            private_key_path=args.private_key.expanduser().resolve(),
            passphrase=passphrase,
        )
        token_type = "KEYPAIR_JWT"

    r = post_statement(
        host=host,
        bearer=bearer,
        token_type=token_type or None,
        statement=statement,
        warehouse=(args.warehouse or None) or None,
        database=(args.database or None) or None,
        schema=(args.schema or None) or None,
        role=(args.role or None) or None,
        timeout=args.timeout,
    )
    print(f"HTTP {r.status_code} {r.url}", file=sys.stderr)
    try:
        out = r.json()
    except json.JSONDecodeError:
        print(r.text[:2000], file=sys.stderr)
        raise SystemExit("Response was not JSON — check host, VPN, and auth headers.")
    print(json.dumps(out, indent=2, ensure_ascii=False))
    if r.status_code >= 400:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
